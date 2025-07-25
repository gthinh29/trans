# logic/google_api.py

import os.path
import re
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import time

# --- Cấu hình ---
SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive.file"]
logger = logging.getLogger(__name__)

# --- CÁC HÀM TIỆN ÍCH CƠ BẢN ---

def get_credentials():
    """Lấy hoặc làm mới thông tin xác thực của người dùng một cách an toàn."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logging.error(f"Lỗi khi làm mới token: {e}. Yêu cầu xác thực lại từ đầu.")
                creds = None # Buộc xác thực lại từ đầu
        if not creds:
            if not os.path.exists("credentials.json"):
                logging.error("Thiếu file credentials.json")
                raise FileNotFoundError("Không tìm thấy file credentials.json để xác thực.")
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return creds

def get_text_from_doc(doc_id):
    """Tải toàn bộ nội dung văn bản từ một Google Doc."""
    try:
        creds = get_credentials()
        service = build("docs", "v1", credentials=creds)
        document = service.documents().get(documentId=doc_id).execute()
        content = document.get('body', {}).get('content', [])
        
        full_text = ""
        for elem in content:
            if 'paragraph' in elem:
                para_elements = elem.get('paragraph', {}).get('elements', [])
                for para_elem in para_elements:
                    if 'textRun' in para_elem:
                        full_text += para_elem.get('textRun', {}).get('content', '')
        return full_text
    except HttpError as e:
        logging.error(f"Lỗi HTTP khi đọc Doc ID: {doc_id} - {e}")
        if e.resp.status == 429:
            logging.warning("Gặp lỗi giới hạn API (429). Đang tạm dừng 60 giây...")
            time.sleep(60)
            return get_text_from_doc(doc_id) # Thử lại
    except Exception as e:
        logging.error(f"Không thể đọc nội dung từ Doc ID: {doc_id} - {e}")
    return None

def extract_id_from_url(url):
    """Trích xuất ID của Google Docs từ một URL."""
    if not url: return None
    match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', url)
    return match.group(1) if match else None

# --- CÁC HÀM LOGIC NÂNG CAO (ĐÃ CẬP NHẬT) ---

def find_relevant_docs(drive_service, chapter_num, novel_name_prefix):
    """
    Tìm tất cả các file Google Docs có khả năng chứa chương được chỉ định.
    Sắp xếp các file theo thời gian sửa đổi gần nhất để ưu tiên các file mới hơn.
    """
    if not novel_name_prefix:
        return []
        
    search_term = novel_name_prefix.replace("'", "\\'")
    # Query tìm các file có tên chứa tên truyện và "Chương" để thu hẹp kết quả
    query = f"mimeType='application/vnd.google-apps.document' and name contains '{search_term}' and name contains 'Chương' and trashed = false"
    fields = "files(id, name, modifiedTime)"
    
    try:
        logging.info(f"Đang tìm kiếm trên Drive cho chương {chapter_num} với tên truyện: '{novel_name_prefix}'")
        response = drive_service.files().list(
            q=query, 
            pageSize=50,  # Giảm pageSize để tránh quá tải, 50 là con số hợp lý
            fields=fields, 
            orderBy="modifiedTime desc" # Ưu tiên file mới sửa đổi
        ).execute()
        
        files = response.get('files', [])
        relevant_files = []

        # Lọc các file có khả năng chứa chương
        for file in files:
            file_name = file.get('name', '')
            # Tìm khoảng chương "Chương X-Y"
            range_match = re.search(r"Chương\s*(\d+)\s*-\s*(\d+)", file_name, re.IGNORECASE)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if start <= chapter_num <= end:
                    relevant_files.append(file)
                    continue # Đã khớp, chuyển sang file tiếp theo

            # Tìm chương đơn "Chương Z"
            single_match = re.findall(r"Chương\s+(\d+)\b", file_name, re.IGNORECASE)
            if single_match and str(chapter_num) in single_match:
                relevant_files.append(file)

        logging.info(f"Tìm thấy {len(relevant_files)} file có khả năng chứa chương {chapter_num}.")
        return relevant_files

    except HttpError as e:
        logging.error(f"Lỗi khi truy vấn Drive: {e}")
        return []


def extract_single_chapter_from_text(full_doc_text, chapter_num):
    """
    Trích xuất nội dung của một chương cụ thể từ văn bản đầy đủ của một tài liệu.
    """
    # Pattern linh hoạt hơn, tìm "Chương" và số, có thể có hoặc không có tên chương
    # ví dụ: "Chương 123: Tên chương", "Chương 123", "chương 123"
    pattern_str = r"(Chương\s+" + str(chapter_num) + r"\b[^\n]*)"
    
    # Sử dụng re.finditer để tìm tất cả các header có thể có
    all_headers = list(re.finditer(r"(Chương\s+\d+\b[^\n]*)", full_doc_text, re.IGNORECASE))
    
    for i, header_match in enumerate(all_headers):
        # Kiểm tra xem header này có phải là của chương chúng ta đang tìm không
        header_text = header_match.group(1)
        num_match = re.search(r"Chương\s+(\d+)", header_text, re.IGNORECASE)
        
        if num_match and int(num_match.group(1)) == chapter_num:
            logging.info(f"Đã tìm thấy tiêu đề cho chương {chapter_num}: '{header_text.strip()}'")
            start_index = header_match.start()
            end_index = len(full_doc_text) # Mặc định là cuối văn bản
            
            # Tìm vị trí bắt đầu của chương tiếp theo để xác định điểm kết thúc
            if i + 1 < len(all_headers):
                end_index = all_headers[i+1].start()
            
            chapter_full_content = full_doc_text[start_index:end_index].strip()
            
            # Tách tiêu đề và nội dung
            lines = chapter_full_content.split('\n', 1)
            title = lines[0].strip()
            content = lines[1].strip() if len(lines) > 1 else ""
            
            return {'title': title, 'content': content}
            
    return None # Không tìm thấy chương trong văn bản


def extract_chapters_from_doc(chapters_to_process, primary_novel_name, doc_link=None, alt_novel_name=None):
    """
    Hàm điều phối chính để trích xuất nội dung chương, được thiết kế lại để xử lý tuần tự và mạnh mẽ hơn.
    - Tìm kiếm chương theo thứ tự từ thấp đến cao.
    - Bỏ qua và báo cáo nếu không tìm thấy chương sau khi đã tìm kiếm qua các file tiềm năng.
    - Dừng lại nếu không tìm thấy 3 chương liên tiếp.
    """
    # Hàm con để xử lý từ link trực tiếp
    def _process_from_link(doc_id, chapters_set):
        logging.info(f"Bắt đầu xử lý từ link trực tiếp cho Doc ID: {doc_id}")
        full_text = get_text_from_doc(doc_id)
        if not full_text:
            logging.error(f"Không thể lấy nội dung từ Doc ID: {doc_id}")
            return {}
        
        extracted = {}
        # Sắp xếp để xử lý tuần tự
        for chap_num in sorted(list(chapters_set)):
            chapter_data = extract_single_chapter_from_text(full_text, chap_num)
            if chapter_data:
                logging.info(f"Trích xuất thành công chương {chap_num} từ link trực tiếp.")
                extracted[chap_num] = chapter_data
            else:
                logging.warning(f"Không tìm thấy chương {chap_num} trong file được cung cấp qua link.")
        return extracted

    # --- Bắt đầu logic chính ---
    if doc_link:
        doc_id = extract_id_from_url(doc_link)
        if not doc_id:
            raise ValueError("Link Google Docs không hợp lệ.")
        return _process_from_link(doc_id, set(chapters_to_process))

    # Trường hợp 2: Tìm kiếm tự động
    creds = get_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    
    all_extracted_chapters = {}
    not_found_chapters = []
    consecutive_failures = 0
    
    # Sắp xếp các chương cần tìm từ thấp đến cao
    sorted_chapters = sorted(chapters_to_process)
    
    for chapter_num in sorted_chapters:
        logging.info(f"--- Bắt đầu xử lý cho Chương {chapter_num} ---")
        
        # Tìm các file tiềm năng cho chương này
        potential_docs = find_relevant_docs(drive_service, chapter_num, primary_novel_name)
        # Nếu không thấy với tên chính, thử tên phụ
        if not potential_docs and alt_novel_name:
            logging.info(f"Không tìm thấy file với tên chính, thử với tên phụ: '{alt_novel_name}'")
            potential_docs = find_relevant_docs(drive_service, chapter_num, alt_novel_name)

        is_chapter_found = False
        if not potential_docs:
             logging.warning(f"Không tìm thấy file Google Docs nào có vẻ chứa chương {chapter_num}.")
        
        # Duyệt qua từng file tiềm năng để tìm chương
        for doc_file in potential_docs:
            doc_id = doc_file.get('id')
            doc_name = doc_file.get('name')
            logging.info(f"Đang kiểm tra file '{doc_name}' (ID: {doc_id}) cho chương {chapter_num}...")
            
            doc_text = get_text_from_doc(doc_id)
            if not doc_text:
                logging.warning(f"Nội dung file '{doc_name}' (ID: {doc_id}) trống hoặc không thể đọc.")
                continue # Chuyển sang file tiếp theo

            chapter_data = extract_single_chapter_from_text(doc_text, chapter_num)
            if chapter_data:
                logging.info(f"✓✓✓ Trích xuất thành công chương {chapter_num} từ file '{doc_name}'.")
                all_extracted_chapters[chapter_num] = chapter_data
                is_chapter_found = True
                consecutive_failures = 0 # Reset bộ đếm khi thành công
                break # Đã tìm thấy chương, không cần kiểm tra các file khác nữa
            else:
                logging.info(f"Không tìm thấy nội dung chương {chapter_num} trong file '{doc_name}'. Tiếp tục tìm ở file khác...")

        if not is_chapter_found:
            logging.error(f"✗✗✗ Không thể trích xuất chương {chapter_num} sau khi đã tìm kiếm tất cả các file tiềm năng.")
            not_found_chapters.append(chapter_num)
            consecutive_failures += 1
            
            if consecutive_failures >= 3:
                logging.critical(f"ĐÃ DỪNG LẠI do không tìm thấy {consecutive_failures} chương liên tiếp.")
                break # Dừng vòng lặp chính

    if not_found_chapters:
        logging.warning("--- TỔNG KẾT ---")
        logging.warning(f"Các chương sau không thể tìm thấy: {sorted(not_found_chapters)}")

    return all_extracted_chapters


# --- CÁC HÀM CŨ (Giữ lại để tương thích nếu cần) ---
# Các hàm cũ không thay đổi, được giữ nguyên ở đây...
def find_doc_id_by_exact_title(doc_title):
    """Tìm ID của một file Google Docs dựa trên tiêu đề chính xác."""
    creds = get_credentials()
    try:
        drive_service = build("drive", "v3", credentials=creds)
        escaped_title = doc_title.replace("'", "\\'")
        query = f"name='{escaped_title}' and mimeType='application/vnd.google-apps.document' and trashed = false"
        response = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = response.get('files', [])
        return files[0].get('id') if files else None
    except Exception as e:
        logging.error(f"Lỗi khi tìm file với tiêu đề '{doc_title}': {e}")
        return None

def replace_text_in_doc(doc_id, new_content):
    """Xóa nội dung cũ và thay bằng nội dung mới."""
    try:
        creds = get_credentials()
        docs_service = build("docs", "v1", credentials=creds)
        doc = docs_service.documents().get(documentId=doc_id).execute()
        doc_content = doc.get('body').get('content')
        if doc_content and len(doc_content) > 1:
            doc_length = doc_content[-1].get('endIndex', 1)
            # Chỉ xóa nếu doc có nội dung (độ dài > 1)
            if doc_length > 1:
                requests = [
                    {'deleteContentRange': {'range': {'startIndex': 1, 'endIndex': doc_length - 1}}},
                    {'insertText': {'location': {'index': 1}, 'text': new_content}}
                ]
            else:
                 requests = [{'insertText': {'location': {'index': 1}, 'text': new_content}}]
        else: # Tài liệu trống
            requests = [{'insertText': {'location': {'index': 1}, 'text': new_content}}]
            
        docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()
        return True
    except Exception as e:
        logging.error(f"Lỗi khi thay thế văn bản: {e}")
        return False