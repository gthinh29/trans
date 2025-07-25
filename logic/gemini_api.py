# logic/gemini_api.py

import google.generativeai as genai
# Sửa lỗi: Bỏ InterruptedError khỏi import của google
from google.generativeai.types import BlockedPromptException
from google.api_core import exceptions as core_exceptions
import logging
import time

# --- Cấu hình ---
logger = logging.getLogger(__name__)

# Sửa lỗi: Định nghĩa Exception tùy chỉnh để thay thế cho InterruptedError đã bị xóa khỏi thư viện
class InterruptedError(Exception):
    """Ngoại lệ cho biết tác vụ đã bị người dùng dừng lại."""
    pass

# Lớp quản lý API Key
class ApiKeyManager:
    def __init__(self, keys_filename="apikeys.txt"):
        self.keys = []
        self.current_key_index = 0
        self.load_keys(keys_filename)

    def load_keys(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                self.keys = [line.strip() for line in f if line.strip()]
            logger.info(f"Đã tải {len(self.keys)} API key.")
        except FileNotFoundError:
            logger.error(f"File {filename} không tồn tại.")
            self.keys = []

    def get_key(self):
        if not self.keys: return None
        return self.keys[self.current_key_index]

    def rotate_key(self):
        if not self.keys: return
        logger.warning(f"Key thứ {self.current_key_index + 1} bị giới hạn. Đang xoay vòng...")
        self.current_key_index = (self.current_key_index + 1) % len(self.keys)

# Khởi tạo các biến toàn cục
api_key_manager = ApiKeyManager()
SAFETY_SETTINGS = {
    "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE", "HARM_CATEGORY_HATE_SPEECH": "BLOCK_NONE",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE", "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_NONE",
}

def stream_gemini_api(prompt_text: str, should_stop_check):
    """
    Hàm gọi API phiên bản ổn định.
    - Xoay vòng key khi gặp lỗi 429 (ResourceExhausted) và thêm thời gian chờ.
    - Xử lý các lỗi khác một cách dứt khoát.
    """
    failed_key_indexes = set()

    while True:
        if should_stop_check():
            raise InterruptedError("Tác vụ đã bị người dùng dừng lại.")
        
        # Cầu dao tự ngắt: Nếu tất cả các key đều đã bị giới hạn 429
        if len(api_key_manager.keys) > 0 and len(failed_key_indexes) >= len(api_key_manager.keys):
            raise core_exceptions.ResourceExhausted("Tất cả các API key đều đã bị giới hạn. Vui lòng thử lại sau.")

        current_key_index = api_key_manager.current_key_index
        current_key = api_key_manager.get_key()
        if not current_key:
            raise ValueError("Không có API key nào trong danh sách.")

        # Bỏ qua key đã bị lỗi 429 trong phiên làm việc này
        if current_key_index in failed_key_indexes:
            api_key_manager.rotate_key()
            continue

        try:
            genai.configure(api_key=current_key)
            model = genai.GenerativeModel('gemini-2.5-pro')
            
            response = model.generate_content(prompt_text, stream=True, safety_settings=SAFETY_SETTINGS)
            
            for chunk in response:
                if should_stop_check():
                    raise InterruptedError("Tác vụ đã bị người dùng dừng lại (giữa các chunk).")
                # Đảm bảo chunk có text và không rỗng trước khi yield
                if hasattr(chunk, 'text') and chunk.text:
                    yield chunk.text
            
            # Nếu stream thành công, thoát khỏi vòng lặp
            return

        except core_exceptions.ResourceExhausted as e:
            # Ghi nhận key này đã bị lỗi 429
            failed_key_indexes.add(current_key_index)
            api_key_manager.rotate_key() # Hàm này đã tự log
            
            # **THAY ĐỔI QUAN TRỌNG**: Thêm thời gian chờ trước khi thử lại
            wait_time = 5 # giây
            logger.info(f"Chờ {wait_time} giây trước khi thử lại với key tiếp theo...")
            time.sleep(wait_time)
            
            continue # Quay lại đầu vòng lặp while để thử key mới
        
        except (BlockedPromptException, core_exceptions.InvalidArgument) as e:
            logger.error(f"Lỗi nội dung hoặc an toàn, tác vụ sẽ bỏ qua chương này: {e}")
            raise BlockedPromptException(f"Nội dung bị chặn hoặc không hợp lệ: {e}") from e
        
        except Exception as e:
            logger.error(f"Gặp lỗi API không mong muốn với key {current_key_index + 1}: {e}", exc_info=True)
            # Với các lỗi không xác định, cũng thử đổi key và chờ
            failed_key_indexes.add(current_key_index)
            api_key_manager.rotate_key()
            
            wait_time = 3 # giây
            logger.info(f"Chờ {wait_time} giây trước khi thử lại...")
            time.sleep(wait_time)
            continue
