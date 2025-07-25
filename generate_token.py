import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive.file"]

def generate_google_token():
    """Chạy script này một lần duy nhất để tạo file token.json."""
    if not os.path.exists("credentials.json"):
        print("LỖI: Không tìm thấy file 'credentials.json'.")
        print("Vui lòng tải file này từ Google Cloud Console và đặt nó vào thư mục gốc.")
        return

    try:
        print("Cần xác thực. Vui lòng đăng nhập vào tài khoản Google của bạn qua trình duyệt...")
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
        print("Xác thực thành công!")
    except Exception as e:
        print(f"Quá trình xác thực thất bại: {e}")
        return

    if creds:
        with open("token.json", "w") as token:
            token.write(creds.to_json())
        print("Đã lưu token xác thực thành công vào file 'token.json'.")

if __name__ == "__main__":
    generate_google_token()