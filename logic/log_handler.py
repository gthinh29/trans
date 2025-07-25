import logging
import threading

# Sử dụng Thread-Local Storage để lưu trữ task_id cho mỗi luồng (thread) riêng biệt.
# Điều này đảm bảo log từ luồng A không bị nhầm vào tác vụ của luồng B.
task_local = threading.local()

class TaskLogHandler(logging.Handler):
    """
    Một logging handler tùy chỉnh, có chức năng gọi một hàm callback (ví dụ: report_progress)
    để báo cáo các thông điệp log cho một tác vụ (task) cụ thể.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Hàm callback sẽ được đăng ký từ bên ngoài (từ app.py)
        self.callback = None

    def set_callback(self, callback_func):
        """
        Đăng ký hàm sẽ được gọi mỗi khi có log mới.
        Trong trường hợp này, chúng ta sẽ đăng ký hàm `report_progress`.
        """
        self.callback = callback_func

    def emit(self, record):
        """
        Hàm này được gọi tự động bởi hệ thống logging mỗi khi có một log mới.
        (ví dụ: khi gọi logging.info(...))
        """
        # Kiểm tra xem callback đã được đăng ký và luồng hiện tại có task_id hay không
        if self.callback and hasattr(task_local, 'task_id'):
            task_id = task_local.task_id
            
            # Định dạng thông điệp log (ví dụ: "2024-07-19... - INFO - Đây là log...")
            formatted_message = self.format(record)
            
            # Gọi hàm callback đã đăng ký với các tham số cần thiết
            # Tương đương với việc gọi: report_progress(task_id, "log", formatted_message)
            self.callback(task_id, "log", formatted_message)

# Tạo một instance duy nhất của handler để toàn bộ ứng dụng sử dụng.
task_log_handler = TaskLogHandler()
