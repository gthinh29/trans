from gevent import monkey
monkey.patch_all()

import os
import json
import logging
import threading
import time
import re
from uuid import uuid4
from bs4 import BeautifulSoup
from google.generativeai.types import BlockedPromptException
from sqlalchemy.exc import IntegrityError

import difflib
from flask import Flask, abort, render_template, request, Response, stream_with_context, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

# Import các module logic
from logic import scraper, gemini_api, google_api

# Import handler tùy chỉnh và biến thread-local để quản lý log theo tác vụ
from logic.log_handler import task_log_handler, task_local

# --- Cấu hình ứng dụng và Database ---
app = Flask(__name__)
# Sửa đổi để sử dụng DATABASE_URL từ môi trường của Render
database_uri = os.environ.get('DATABASE_URL')
if database_uri and database_uri.startswith("postgres://"):
    database_uri = database_uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_uri or 'sqlite:///' + os.path.join(app.instance_path, 'app.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'a-very-secret-key-for-flash-messages'

db = SQLAlchemy(app)
migrate = Migrate(app, db)

try:
    os.makedirs(app.instance_path)
except OSError:
    pass

# --- Cấu hình Logging Toàn cục ---
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

if root_logger.hasHandlers():
    root_logger.handlers.clear()

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
root_logger.addHandler(stream_handler)

file_handler = logging.FileHandler("history.log", encoding='utf-8')
file_handler.setFormatter(formatter)
root_logger.addHandler(file_handler)

task_log_handler.set_callback(lambda task_id, type, content: report_progress(task_id, type, content))
task_log_handler.setFormatter(formatter)
root_logger.addHandler(task_log_handler)


# Các biến toàn cục để quản lý tác vụ
TASK_PROGRESS = {}
TASK_CONTROL = {}

# ===================================================================
# CÁC MODEL DATABASE
# ===================================================================
class Novel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    author = db.Column(db.String(100))
    description = db.Column(db.Text)
    cover_image = db.Column(db.String(255))
    source_url = db.Column(db.String(512), nullable=True)
    chapters = db.relationship('Chapter', backref='novel', lazy=True, cascade="all, delete-orphan")
    tags = db.relationship('Tag', secondary='tags', lazy='subquery',
        backref=db.backref('novels', lazy=True))

class Chapter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chapter_number = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    original_content = db.Column(db.Text)
    translated_content = db.Column(db.Text)
    novel_id = db.Column(db.Integer, db.ForeignKey('novel.id'), nullable=False)

tags = db.Table('tags',
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True),
    db.Column('novel_id', db.Integer, db.ForeignKey('novel.id'), primary_key=True)
)

class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

class ChapterHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey('chapter.id'), nullable=False)
    translated_content_old = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    
    chapter = db.relationship('Chapter', backref=db.backref('history_entries', lazy=True, cascade="all, delete-orphan"))

# ===================================================================
# CÁC HÀM TIỆN ÍCH
# ===================================================================

def nl2p(text):
    """
    Chuyển đổi văn bản thuần có dấu xuống dòng (newline) thành các
    đoạn văn bản HTML được bao bọc bởi thẻ <p>.
    Điều này rất quan trọng để các ứng dụng hỗ trợ (như Audify)
    có thể nhận diện chính xác từng đoạn văn.
    """
    if not text:
        return ""
    # Tách văn bản thành các đoạn dựa trên các dòng trống (một hoặc nhiều dấu xuống dòng)
    paragraphs = re.split(r'\n\s*\n', text.strip())
    # Bao bọc mỗi đoạn bằng thẻ <p> và nối chúng lại
    html_output = ''.join(f'<p>{p.strip()}</p>' for p in paragraphs if p.strip())
    return html_output

# Đăng ký hàm trên như một "bộ lọc" (filter) để có thể dùng trong file HTML
app.jinja_env.filters['nl2p'] = nl2p

def generate_grid_diff(old_text, new_text):
    """
    Tạo ra một cấu trúc HTML dùng CSS Grid để so sánh 2 văn bản.
    Đây là giải pháp thay thế cho difflib.HtmlDiff().make_table() để
    đảm bảo layout luôn vừa vặn với màn hình.
    """
    # Đảm bảo đầu vào là chuỗi, tránh lỗi None.splitlines()
    old_text = old_text or ""
    new_text = new_text or ""

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)

    left_col_html = []
    right_col_html = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for i in range(i1, i2):
                # Thêm thẻ span để giữ cấu trúc dòng
                left_col_html.append(f"<span>{old_lines[i] if old_lines[i] else ' '}</span>")
            for i in range(j1, j2):
                right_col_html.append(f"<span>{new_lines[i] if new_lines[i] else ' '}</span>")

        elif tag == 'replace':
            for i in range(i1, i2):
                # Dùng class diff_sub cho dòng bị thay thế
                left_col_html.append(f'<span class="diff_sub">{old_lines[i] if old_lines[i] else " "}</span>')
            for i in range(j1, j2):
                # Dùng class diff_add cho dòng thay thế
                right_col_html.append(f'<span class="diff_add">{new_lines[i] if new_lines[i] else " "}</span>')

        elif tag == 'delete':
            for i in range(i1, i2):
                # Dùng class diff_sub cho dòng bị xóa
                left_col_html.append(f'<span class="diff_sub">{old_lines[i] if old_lines[i] else " "}</span>')
            # Thêm dòng trống ở cột phải để giữ đúng vị trí
            for _ in range(i1, i2):
                right_col_html.append('<span></span>')

        elif tag == 'insert':
            for i in range(j1, j2):
                # Dùng class diff_add cho dòng được thêm
                right_col_html.append(f'<span class="diff_add">{new_lines[i] if new_lines[i] else " "}</span>')
            # Thêm dòng trống ở cột trái để giữ đúng vị trí
            for _ in range(j1, j2):
                left_col_html.append('<span></span>')

    # Nối các dòng lại và bọc trong thẻ <pre> để giữ định dạng
    final_left_html = "<pre>" + "\n".join(left_col_html) + "</pre>"
    final_right_html = "<pre>" + "\n".join(right_col_html) + "</pre>"

    # Tạo cấu trúc Grid cuối cùng
    final_html = f"""
    <div class="diff-grid-container">
        <div class="diff-column">
            <div class="diff-header">Phiên bản cũ</div>
            {final_left_html}
        </div>
        <div class="diff-column">
            <div class="diff-header">Phiên bản hiện tại</div>
            {final_right_html}
        </div>
    </div>
    """
    return final_html

def get_prompt(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f: return f.read().strip()
    except FileNotFoundError:
        logging.warning(f"File prompt '{filename}' không tồn tại.")
        return "Translate the following to Vietnamese."

def parse_chapter_input(chapter_input):
    if not chapter_input: return None
    chapter_input = chapter_input.strip()
    if '-' in chapter_input:
        parts = chapter_input.split('-')
        if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            start, end = int(parts[0].strip()), int(parts[1].strip())
            return list(range(start, end + 1)) if start <= end else None
    elif chapter_input.isdigit():
        return [int(chapter_input)]
    return None

def report_progress(task_id, type, content):
    if task_id not in TASK_PROGRESS: TASK_PROGRESS[task_id] = []
    TASK_PROGRESS[task_id].append(json.dumps({"type": type, "content": content}))

def should_stop(task_id):
    return TASK_CONTROL.get(task_id, {}).get('stop', False)

# ===================================================================
# CÁC HÀM LOGIC CHẠY NỀN (ĐÃ SỬA LỖI)
# ===================================================================
def run_translate_to_db_thread(task_id, novel_id, start_chapter, end_chapter):
    task_local.task_id = task_id
    with app.app_context():
        try:
            logging.info(f"Bắt đầu dịch & lưu vào DB cho truyện ID {novel_id} từ chương {start_chapter}-{end_chapter}.")
            
            novel = Novel.query.get(novel_id)
            if not novel:
                raise Exception(f"Không tìm thấy truyện với ID {novel_id}.")
            if not novel.source_url:
                raise Exception(f"Truyện ID {novel_id} không có URL nguồn.")

            prompt = get_prompt("prompt.txt")

            for chapter_num in range(start_chapter, end_chapter + 1):
                should_stop_check = lambda: should_stop(task_id)
                if should_stop_check():
                    logging.info("Tác vụ bị người dùng dừng.")
                    break

                report_progress(task_id, "status", f"Đang kiểm tra chương {chapter_num}...")
                existing_chapter = Chapter.query.filter_by(novel_id=novel_id, chapter_number=chapter_num).first()
                
                if existing_chapter and existing_chapter.translated_content:
                    report_progress(task_id, "log", f"-> Bỏ qua chương {chapter_num} vì đã có bản dịch.")
                    time.sleep(0.1)
                    continue
                
                try:
                    report_progress(task_id, "status", f"Đang lấy dữ liệu web cho chương {chapter_num}...")
                    title, story = scraper.get_chapter_data(novel.source_url, chapter_num)
                    
                    if not story:
                        raise ValueError("Không lấy được nội dung gốc từ web.")

                    chapter_to_update = existing_chapter if existing_chapter else Chapter(novel_id=novel_id, chapter_number=chapter_num)
                    if not existing_chapter:
                        db.session.add(chapter_to_update)
                    
                    chapter_to_update.title = title or f"Chương {chapter_num}"
                    chapter_to_update.original_content = story
                    
                    report_progress(task_id, "original_text_append", f"\n\n--- CHƯƠNG {chapter_num} (GỐC) ---\n{title}\n\n{story}")
                    report_progress(task_id, "translated_text_append", f"\n\n--- CHƯƠNG {chapter_num} (DỊCH MỚI) ---\n")
                    
                    report_progress(task_id, "status", f"Đang dịch chương {chapter_num}...")
                    prompt_for_ai = f"{prompt}\n\n{title}\n\n{story}"
                    
                    new_translation = ""
                    for chunk in gemini_api.stream_gemini_api(prompt_for_ai, should_stop_check):
                        report_progress(task_id, "translated_chunk", chunk)
                        new_translation += chunk
                    
                    if should_stop_check():
                        db.session.rollback()
                        logging.info(f"Tác vụ dừng, không lưu chương {chapter_num}.")
                        break

                    chapter_to_update.translated_content = new_translation
                    db.session.commit()
                    report_progress(task_id, "log", f"Đã dịch và lưu thành công chương {chapter_num}.")
                
                except (BlockedPromptException, InterruptedError, ValueError, IntegrityError) as e:
                    db.session.rollback()
                    log_msg = f"-> Bỏ qua chương {chapter_num} do: {e}"
                    report_progress(task_id, "log", log_msg)
                    logging.warning(log_msg)
                    continue

            final_message = "Đã dừng." if should_stop(task_id) else "Hoàn tất dịch!"
            report_progress(task_id, "final", final_message)

        except Exception as e:
            db.session.rollback()
            logging.error(f"Lỗi nghiêm trọng trong tác vụ Dịch {task_id}: {e}", exc_info=True)
            report_progress(task_id, "error", str(e))
        finally:
            if hasattr(task_local, 'task_id'):
                del task_local.task_id

def run_auto_translate_thread(task_id, novel_id, start_chapter, save_to_docs, doc_id):
    task_local.task_id = task_id
    with app.app_context():
        try:
            logging.info(f"Bắt đầu 'Tự Động Dịch' từ chương {start_chapter}. Lưu ra Docs: {save_to_docs}.")
            novel = Novel.query.get(novel_id)
            if not novel:
                raise Exception(f"Không tìm thấy truyện với ID {novel_id}")

            report_progress(task_id, "status", f"Bắt đầu quá trình cho truyện '{novel.title}'...")
            current_chapter = start_chapter
            session_doc_id = doc_id
            chapters_in_doc = 0
            
            while not should_stop(task_id):
                report_progress(task_id, "status", f"Đang lấy dữ liệu web cho chương {current_chapter}...")
                
                title, story = scraper.get_chapter_data(novel.source_url, current_chapter)
                prompt_for_ai = f"{get_prompt('prompt.txt')}\n\n{title}\n\n{story}"
                full_translated = ""
                
                should_stop_check = lambda: should_stop(task_id)
                for chunk in gemini_api.stream_gemini_api(prompt_for_ai, should_stop_check):
                    report_progress(task_id, "translated_chunk", chunk)
                    full_translated += chunk
                
                if should_stop(task_id): break

                if save_to_docs:
                    report_progress(task_id, "status", f"Đang lưu chương {current_chapter} vào Google Docs...")
                    doc_title = ""
                    if chapters_in_doc == 0 and not session_doc_id:
                        doc_title = f"{novel.title} - Chương {current_chapter}-{current_chapter+9}"
                    
                    session_doc_id = google_api.save_to_google_docs(session_doc_id, doc_title, full_translated)
                    report_progress(task_id, "status", f"Đã lưu thành công vào Doc ID: {session_doc_id}.")
                    chapters_in_doc = (chapters_in_doc + 1) % 10
                else:
                    chapter_to_update = Chapter.query.filter_by(novel_id=novel_id, chapter_number=current_chapter).first()
                    if not chapter_to_update:
                        chapter_to_update = Chapter(novel_id=novel_id, chapter_number=current_chapter)
                        db.session.add(chapter_to_update)
                    chapter_to_update.title = title or f"Chương {current_chapter}"
                    chapter_to_update.original_content = story
                    chapter_to_update.translated_content = full_translated
                    db.session.commit()
                    report_progress(task_id, "status", f"Đã lưu thành công chương {current_chapter} vào Database.")

                current_chapter += 1
                report_progress(task_id, "status", "Chờ 5 giây trước khi tiếp tục...")
                time.sleep(5)
                
            report_progress(task_id, "final", "Quá trình tự động đã dừng.")
        except Exception as e:
            db.session.rollback()
            logging.error(f"Lỗi trong tác vụ tự động dịch {task_id}: {e}", exc_info=True)
            report_progress(task_id, "error", str(e))
        finally:
            if hasattr(task_local, 'task_id'):
                del task_local.task_id

def run_proofread_db_thread(task_id, novel_id, chapters_to_process):
    task_local.task_id = task_id
    with app.app_context():
        try:
            # Lấy novel object để truy cập source_url
            novel = Novel.query.get(novel_id)
            if not novel:
                raise Exception(f"Không tìm thấy truyện với ID {novel_id}.")
            if not novel.source_url:
                raise Exception(f"Truyện '{novel.title}' không có URL nguồn để đối chiếu bản gốc.")

            logging.info(f"Bắt đầu Cải thiện bản dịch cho truyện '{novel.title}', chương: {chapters_to_process}.")
            prompt_template = get_prompt('prompt_ct.txt')

            for chapter_num in chapters_to_process:
                should_stop_check = lambda: should_stop(task_id)
                if should_stop_check():
                    logging.info(f"Tác vụ bị người dùng dừng.")
                    break
                
                report_progress(task_id, "status", f"Đang xử lý chương {chapter_num}...")
                chapter = Chapter.query.filter_by(novel_id=novel_id, chapter_number=chapter_num).first()
                
                if not chapter or not chapter.translated_content:
                    report_progress(task_id, "log", f"-> Bỏ qua chương {chapter_num} vì không tìm thấy hoặc chưa được dịch.")
                    continue

                try:
                    # --- BƯỚC 1: SCRAPE BẢN GỐC TIẾNG ANH ---
                    report_progress(task_id, "status", f"Đang scrap bản gốc Tiếng Anh cho chương {chapter_num}...")
                    
                    # Gọi hàm từ scraper.py
                    original_title, original_story = scraper.get_chapter_data(novel.source_url, chapter_num)
                    
                    if not original_story:
                        report_progress(task_id, "log", f"-> Lỗi: Không lấy được bản gốc chương {chapter_num} từ web. Bỏ qua.")
                        continue # Bỏ qua chương này nếu không scrape được
                    
                    # Ghép tiêu đề và nội dung thành bản gốc hoàn chỉnh
                    english_text = f"{original_title}\n\n{original_story}"
                    
                    # --- BƯỚC 2: CHUẨN BỊ PROMPT VÀ GỌI API ---
                    original_header = f"\n\n{'='*20} BẢN GỐC (CHƯƠNG {chapter_num}) {'='*20}\n\n"
                    report_progress(task_id, "original_text_append", original_header + english_text)
                    
                    translated_header = f"\n\n{'='*20} BẢN DỊCH MỚI (CHƯƠNG {chapter_num}) {'='*20}\n\n"
                    report_progress(task_id, "translated_text_append", translated_header)
                    
                    # Điền cả bản gốc và bản dịch cũ vào prompt mới
                    prompt_for_ai = prompt_template.format(
                        english_text=english_text, 
                        text_to_proofread=chapter.translated_content
                    )
                    
                    new_translation = ""
                    
                    report_progress(task_id, "status", f"Đang gửi yêu cầu cải thiện cho chương {chapter_num}...")
                    for chunk in gemini_api.stream_gemini_api(prompt_for_ai, should_stop_check):
                        report_progress(task_id, "translated_chunk", chunk)
                        new_translation += chunk
                    
                    # --- BƯỚC 3: LƯU KẾT QUẢ ---
                    if not should_stop_check():
                        if new_translation.strip():
                            history_entry = ChapterHistory(
                                chapter_id=chapter.id,
                                translated_content_old=chapter.translated_content
                            )
                            db.session.add(history_entry)
                            chapter.translated_content = new_translation
                            db.session.commit()
                            report_progress(task_id, "log", f"Đã cải thiện và lưu thành công chương {chapter_num}.")
                        else:
                            report_progress(task_id, "log", f"Không có nội dung mới được tạo cho chương {chapter_num}. Không lưu.")
                            db.session.rollback()

                except (BlockedPromptException, InterruptedError) as e:
                    db.session.rollback()
                    log_msg = f"-> Bỏ qua chương {chapter_num} do: {e}"
                    report_progress(task_id, "log", log_msg)
                    logging.warning(log_msg)
                    continue

            final_message = "Đã dừng." if should_stop(task_id) else "Hoàn tất cải thiện bản dịch!"
            report_progress(task_id, "final", final_message)

        except Exception as e:
            db.session.rollback()
            logging.error(f"Lỗi nghiêm trọng trong tác vụ Cải thiện {task_id}: {e}", exc_info=True)
            report_progress(task_id, "error", f"Đã xảy ra lỗi nghiêm trọng: {e}")
        finally:
            if hasattr(task_local, 'task_id'):
                del task_local.task_id

def run_load_from_docs_thread(task_id, novel_id, chapters_to_process, doc_link, alt_novel_name):
    task_local.task_id = task_id
    with app.app_context():
        try:
            logging.info(f"Bắt đầu tải từ Docs. Novel ID: {novel_id}, Chapters: {chapters_to_process}, Link: {doc_link}, Alt Name: {alt_novel_name}")
            novel = Novel.query.get(novel_id)
            if not novel:
                raise Exception(f"Không tìm thấy truyện với ID: {novel_id}")
            
            report_progress(task_id, "status", "Bắt đầu quá trình tải từ Google Docs...")
            
            extracted_chapters = google_api.extract_chapters_from_doc(
                chapters_to_process=chapters_to_process,
                primary_novel_name=novel.title,
                doc_link=doc_link,
                alt_novel_name=alt_novel_name
            )

            if not extracted_chapters:
                raise Exception("Không trích xuất được chương nào từ Google Docs.")

            saved_count = 0
            for chapter_num, data in sorted(extracted_chapters.items()):
                if should_stop(task_id): 
                    break
                
                report_progress(task_id, "status", f"Đang xử lý chương {chapter_num} để lưu vào DB...")
                
                chapter = Chapter.query.filter_by(novel_id=novel_id, chapter_number=chapter_num).first()
                if not chapter:
                    chapter = Chapter(novel_id=novel_id, chapter_number=chapter_num)
                    db.session.add(chapter)
                    logging.info(f"-> Tạo chương mới trong DB: {chapter_num}")
                
                chapter.title = data.get('title') or f"Chương {chapter_num}"
                chapter.translated_content = f"{chapter.title}\n\n{data.get('content', '')}"
                
                logging.info(f"-> Đã cập nhật tiêu đề và nội dung cho chương {chapter_num}.")
                saved_count += 1

            if not should_stop(task_id) and saved_count > 0:
                report_progress(task_id, "status", "Đang lưu tất cả thay đổi vào database...")
                db.session.commit()
                logging.info(f"ĐÃ LƯU THÀNH CÔNG {saved_count} CHƯƠNG VÀO DATABASE!")

            final_message = "Đã dừng." if should_stop(task_id) else "Hoàn tất tải và lưu chương từ Google Docs!"
            report_progress(task_id, "final", final_message)
            
        except Exception as e:
            db.session.rollback()
            logging.error(f"Lỗi trong tác vụ tải chương {task_id}: {e}", exc_info=True)
            report_progress(task_id, "error", str(e))
        finally:
            if hasattr(task_local, 'task_id'):
                del task_local.task_id

def run_fix_docs_thread(task_id, title_prefix, start_chapter):
    task_local.task_id = task_id
    with app.app_context():
        try:
            logging.info(f"Bắt đầu 'Sửa HTML' với tiền tố '{title_prefix}' từ chương {start_chapter}.")
            current_chapter = start_chapter
            while not should_stop(task_id):
                end_chapter = current_chapter + 9
                
                doc_title = f"{title_prefix} - Chương {current_chapter}-{end_chapter}"
                
                logging.info(f"Đang tìm file có tên chính xác: '{doc_title}'...")
                
                doc_id = google_api.find_doc_id_by_exact_title(doc_title)

                if not doc_id:
                    logging.error(f"Không tìm thấy file '{doc_title}'. Dừng quá trình.")
                    break
                
                logging.info(f"Đã tìm thấy file ID: {doc_id}. Đang đọc nội dung...")
                content = google_api.get_text_from_doc(doc_id)
                
                if not content:
                    logging.warning(f"Nội dung file rỗng. Bỏ qua và chuyển sang cụm chương tiếp theo.")
                    current_chapter += 10
                    continue

                cleaned_content = BeautifulSoup(content, "html.parser").get_text()
                
                if content.strip() == cleaned_content.strip():
                    logging.info(f"File '{doc_title}' đã sạch, không cần sửa. Bỏ qua.")
                else:
                    logging.info(f"Phát hiện mã HTML. Đang cập nhật lại file...")
                    google_api.replace_text_in_doc(doc_id, cleaned_content)
                    logging.info(f"Đã sửa xong file: '{doc_title}'.")
                
                current_chapter += 10
                logging.info("Chờ 2 giây...")
                time.sleep(2)

            report_progress(task_id, "final", "Đã dừng." if should_stop(task_id) else "Hoàn tất sửa HTML!")
        except Exception as e:
            logging.error(f"Lỗi trong tác vụ sửa HTML {task_id}: {e}", exc_info=True)
            report_progress(task_id, "error", str(e))
        finally:
            if hasattr(task_local, 'task_id'):
                del task_local.task_id

# ===================================================================
# CÁC ROUTE VÀ VIEW
# ===================================================================
@app.route("/")
def public_index():
    novels = Novel.query.order_by(Novel.title).all()
    return render_template("public_index.html", novels=novels)

@app.route("/novel/<int:novel_id>")
def public_novel(novel_id):
    novel = Novel.query.get_or_404(novel_id)
    chapters = Chapter.query.filter_by(novel_id=novel.id).order_by(Chapter.chapter_number).all()
    first_chapter = Chapter.query.filter_by(novel_id=novel.id).order_by(Chapter.chapter_number.asc()).first()
    last_chapter = Chapter.query.filter_by(novel_id=novel.id).order_by(Chapter.chapter_number.desc()).first()
    return render_template(
        "public_novel.html", 
        novel=novel, 
        chapters=chapters,
        first_chapter_id=first_chapter.id if first_chapter else None,
        last_chapter_id=last_chapter.id if last_chapter else None
    )

@app.route("/chapter/<int:chapter_id>")
def read_chapter(chapter_id):
    chapter = Chapter.query.get_or_404(chapter_id)
    prev_chapter = Chapter.query.filter(
        Chapter.novel_id == chapter.novel_id,
        Chapter.chapter_number < chapter.chapter_number
    ).order_by(Chapter.chapter_number.desc()).first()
    next_chapter = Chapter.query.filter(
        Chapter.novel_id == chapter.novel_id,
        Chapter.chapter_number > chapter.chapter_number
    ).order_by(Chapter.chapter_number.asc()).first()
    return render_template(
        "read_chapter.html", 
        chapter=chapter,
        prev_chapter_id=prev_chapter.id if prev_chapter else None,
        next_chapter_id=next_chapter.id if next_chapter else None
    )

@app.route("/manage")
def manage_dashboard():
    novels = Novel.query.all()
    return render_template("manage/dashboard.html", novels=novels)

@app.route("/manage/novel/add", methods=['GET', 'POST'])
def add_novel():
    if request.method == 'POST':
        new_novel = Novel(
            title=request.form['title'],
            author=request.form.get('author'),
            description=request.form.get('description'),
            cover_image=request.form.get('cover_image'),
            source_url=request.form.get('source_url')
            
        )
        tag_string = request.form.get('tags', '')
        if tag_string:
            tag_names = [name.strip() for name in tag_string.split(',') if name.strip()]
            for name in tag_names:
                tag = Tag.query.filter_by(name=name).first()
                if not tag:
                    tag = Tag(name=name)
                    db.session.add(tag)
                new_novel.tags.append(tag)
        db.session.add(new_novel)
        db.session.commit()
        flash('Đã thêm truyện mới thành công!', 'success')
        return redirect(url_for('manage_dashboard'))
    return render_template("manage/novel_form.html", novel=None, title="Thêm truyện mới")

@app.route("/manage/novel/<int:novel_id>/edit", methods=['GET', 'POST'])
def edit_novel(novel_id):
    novel = Novel.query.get_or_404(novel_id)
    if request.method == 'POST':
        novel.title = request.form['title']
        novel.author = request.form.get('author')
        novel.description = request.form.get('description')
        novel.cover_image = request.form.get('cover_image')
        novel.source_url = request.form.get('source_url')
        novel.tags.clear()
        tag_string = request.form.get('tags', '')
        if tag_string:
            tag_names = [name.strip() for name in tag_string.split(',') if name.strip()]
            for name in tag_names:
                tag = Tag.query.filter_by(name=name).first()
                if not tag:
                    tag = Tag(name=name)
                    db.session.add(tag)
                novel.tags.append(tag)
        db.session.commit()
        flash('Đã cập nhật thông tin truyện!', 'success')
        return redirect(url_for('manage_novel_detail', novel_id=novel.id))
    
    existing_tags = ', '.join([tag.name for tag in novel.tags])
    return render_template("manage/novel_form.html", novel=novel, title="Chỉnh sửa truyện", existing_tags=existing_tags)

@app.route("/manage/novel/<int:novel_id>")
def manage_novel_detail(novel_id):
    novel = Novel.query.get_or_404(novel_id)
    chapters = Chapter.query.filter_by(novel_id=novel.id).order_by(Chapter.chapter_number).all()
    return render_template("manage/novel_detail.html", novel=novel, chapters=chapters)

@app.route("/manage/novel/<int:novel_id>/delete", methods=['POST'])
def delete_novel(novel_id):
    novel = Novel.query.get_or_404(novel_id)
    db.session.delete(novel)
    db.session.commit()
    flash('Đã xóa truyện thành công!', 'success')
    return redirect(url_for('manage_dashboard'))

@app.route("/manage/novel/<int:novel_id>/add_chapter", methods=['POST'])
def add_chapter(novel_id):
    chapter_number = request.form.get('chapter_number', type=int)
    title = request.form.get('title', f'Chương {chapter_number}')
    if chapter_number:
        new_chapter = Chapter(novel_id=novel_id, chapter_number=chapter_number, title=title)
        db.session.add(new_chapter)
        db.session.commit()
        flash('Đã thêm chương mới.', 'success')
    return redirect(url_for('manage_novel_detail', novel_id=novel_id))

@app.route("/manage/chapter/<int:chapter_id>/delete", methods=['POST'])
def delete_chapter(chapter_id):
    chapter = Chapter.query.get_or_404(chapter_id)
    novel_id = chapter.novel_id
    db.session.delete(chapter)
    db.session.commit()
    flash('Đã xóa chương.', 'success')
    return redirect(url_for('manage_novel_detail', novel_id=novel_id))

@app.route("/manage/chapter/<int:chapter_id>/edit", methods=['GET', 'POST'])
def edit_chapter(chapter_id):
    chapter = Chapter.query.get_or_404(chapter_id)
    if request.method == 'POST':
        updated_content = request.form.get('content')
        if chapter.translated_content != updated_content:
            history_entry = ChapterHistory(
                chapter_id=chapter.id,
                translated_content_old=chapter.translated_content
            )
            db.session.add(history_entry)
            chapter.translated_content = updated_content
            db.session.commit()
            flash(f'Đã cập nhật thành công chương {chapter.chapter_number}!', 'success')
        else:
            flash('Nội dung không có gì thay đổi.', 'info')
        
        return redirect(url_for('manage_novel_detail', novel_id=chapter.novel_id))

    content_for_editor = chapter.translated_content or ""
    return render_template("manage/edit_chapter.html", chapter=chapter, content_for_editor=content_for_editor)

@app.route("/manage/chapters/delete_multiple", methods=['POST'])
def delete_multiple_chapters():
    chapter_ids_to_delete = request.form.getlist('chapter_ids[]')
    if not chapter_ids_to_delete:
        flash('Bạn chưa chọn chương nào để xóa.', 'warning')
        return redirect(request.referrer or url_for('manage_dashboard'))

    first_chapter = Chapter.query.get(chapter_ids_to_delete[0])
    if not first_chapter:
        flash('Lỗi: Không tìm thấy chương để xác định truyện.', 'error')
        return redirect(url_for('manage_dashboard'))
    
    novel_id = first_chapter.novel_id
    deleted_count = 0
    for chapter_id in chapter_ids_to_delete:
        chapter = Chapter.query.get(chapter_id)
        if chapter and chapter.novel_id == novel_id:
            db.session.delete(chapter)
            deleted_count += 1
            
    db.session.commit()
    flash(f'Đã xóa thành công {deleted_count} chương!', 'success')
    return redirect(url_for('manage_novel_detail', novel_id=novel_id))

@app.route("/manage/live-view")
def live_view():
    task_id = request.args.get('task_id')
    title = request.args.get('title', 'Theo dõi tiến trình')
    view_type = request.args.get('view_type', 'dual_pane')
    return render_template("manage/live_view.html", task_id=task_id, title=title, view_type=view_type)

@app.route("/start-task", methods=['POST'])
def start_task():
    task_type = request.form.get('task_type')
    task_id = str(uuid4())
    TASK_CONTROL[task_id] = {'stop': False}
    thread = None
    title_for_live_view = "Live Task"
    view_type = "dual_pane"
    novel_id = request.form.get('novel_id', type=int)

    if task_type == 'translate_to_db':
        start_chapter = request.form.get('start_chapter', type=int)
        end_chapter = request.form.get('end_chapter', type=int)
        if novel_id and start_chapter and end_chapter:
            thread = threading.Thread(target=run_translate_to_db_thread, args=(task_id, novel_id, start_chapter, end_chapter))
            title_for_live_view = f"Dịch vào Web ({start_chapter}-{end_chapter})"
    elif task_type == 'auto_translate':
        start_chapter = request.form.get('start_chapter', 1, type=int)
        save_to_docs = request.form.get('save_to_docs') == 'on'
        doc_id = request.form.get('doc_id')
        if novel_id:
            thread = threading.Thread(target=run_auto_translate_thread, args=(task_id, novel_id, start_chapter, save_to_docs, doc_id))
            title_for_live_view = "Tự Động Dịch"
    elif task_type == 'proofread_db':
        chapters = parse_chapter_input(request.form.get('chapters'))
        if novel_id and chapters:
            thread = threading.Thread(target=run_proofread_db_thread, args=(task_id, novel_id, chapters))
            title_for_live_view = f"Cải thiện bản dịch DB"
    elif task_type == 'fix_docs':
        start_chapter = request.form.get('start_chapter', 1, type=int)
        title_prefix = request.form.get('title_prefix')
        if not title_prefix:
            return {"error": "Vui lòng nhập Tiền tố tên truyện để sửa."}, 400
        thread = threading.Thread(target=run_fix_docs_thread, args=(task_id, title_prefix, start_chapter))
        title_for_live_view = f"Sửa HTML Docs ({title_prefix})"
        view_type = "log"
    elif task_type == 'load_from_docs':
        chapters = parse_chapter_input(request.form.get('chapters'))
        doc_link = request.form.get('doc_link')
        alt_novel_name = request.form.get('alt_novel_name')
        if novel_id and chapters:
            thread = threading.Thread(target=run_load_from_docs_thread, args=(task_id, novel_id, chapters, doc_link, alt_novel_name))
            title_for_live_view = f"Tải từ Docs ({request.form.get('chapters')})"
            view_type = "log"

    if thread:
        thread.daemon = True
        thread.start()
        live_view_url = url_for('live_view', task_id=task_id, title=title_for_live_view, view_type=view_type)
        return {"task_id": task_id, "redirect_url": live_view_url}
    return {"error": "Loại tác vụ không hợp lệ hoặc thiếu tham số."}, 400

@app.route("/stop-task/<task_id>", methods=['POST'])
def stop_task(task_id):
    if task_id in TASK_CONTROL:
        TASK_CONTROL[task_id]['stop'] = True
        logging.info(f"Nhận được yêu cầu dừng cho tác vụ {task_id}")
        return {"status": "stopping"}
    return {"status": "not_found"}, 404

def generate_stream_for_task(task_id):
    last_sent_index = 0
    while True:
        if task_id in TASK_PROGRESS:
            messages = TASK_PROGRESS[task_id]
            for i in range(last_sent_index, len(messages)):
                yield f"data: {messages[i]}\n\n"
            last_sent_index = len(messages)
            
            try:
                last_message_data = json.loads(messages[-1])
                if last_message_data.get('type') in ['final', 'error']:
                    time.sleep(1)
                    if task_id in TASK_PROGRESS: del TASK_PROGRESS[task_id]
                    if task_id in TASK_CONTROL: del TASK_CONTROL[task_id]
                    break
            except (json.JSONDecodeError, IndexError):
                pass

        time.sleep(0.1)
        if task_id not in TASK_CONTROL and task_id not in TASK_PROGRESS:
            break

@app.route("/task-stream/<task_id>")
def task_stream(task_id):
    return Response(stream_with_context(generate_stream_for_task(task_id)), mimetype="text/event-stream")

@app.route("/manage/chapter/<int:chapter_id>/history")
def chapter_history(chapter_id):
    chapter = Chapter.query.get_or_404(chapter_id)
    history_entries = ChapterHistory.query.filter_by(chapter_id=chapter.id).order_by(ChapterHistory.created_at.desc()).all()
    
    diffs = []
    for entry in history_entries:
        # SỬ DỤNG HÀM MỚI ĐỂ TẠO HTML
        diff_html_output = generate_grid_diff(
            entry.translated_content_old, 
            chapter.translated_content
        )
        # Gửi biến mới qua template
        diffs.append({"entry": entry, "diff_html": diff_html_output})

    return render_template("manage/history.html", chapter=chapter, diffs=diffs)

@app.route("/manage/history/<int:history_id>/revert", methods=['POST'])
def revert_chapter(history_id):
    history_entry = ChapterHistory.query.get_or_404(history_id)
    chapter = history_entry.chapter

    # Lấy nội dung cũ từ lịch sử để hoàn tác
    content_to_revert_to = history_entry.translated_content_old

    # Lưu phiên bản HIỆN TẠI vào lịch sử trước khi hoàn tác
    # để người dùng có thể "hoàn tác lại hành động hoàn tác"
    new_history_for_current = ChapterHistory(
        chapter_id=chapter.id,
        translated_content_old=chapter.translated_content
    )
    db.session.add(new_history_for_current)

    # Cập nhật nội dung chương về phiên bản cũ
    chapter.translated_content = content_to_revert_to
    
    db.session.commit()
    flash(f"Đã hoàn tác thành công chương {chapter.chapter_number} về phiên bản lúc {history_entry.created_at.strftime('%Y-%m-%d %H:%M:%S')}.", "success")
    return redirect(url_for('chapter_history', chapter_id=chapter.id))

# --- Main execution ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000, threaded=True)
