import requests
from bs4 import BeautifulSoup
import logging

# --- Cấu hình Scraping API (Đã hoàn lại theo yêu cầu) ---
# Giữ nguyên logic key như phiên bản gốc của bạn.
SCRAPING_API_KEYS = [
    {"name": "ScraperAPI", "key": "c44d4b74755d8d58d5b4ff9712efe0b0"},
    {"name": "ScrapingBee", "key": "RCZMD6OZA5X5LD4WJA5Z5AYD6Z6AW4TCUB82FJ98QE4A68N05UBFDHLRZIP2YLJ92JRUXEPC6EVW6QCG"},
]
current_scraping_key_index = 0

# --- Hàm lấy dữ liệu chương ---

def get_chapter_data(base_url: str, chapter_number: int) -> tuple[str, str]:
    """
    Lấy dữ liệu chương bằng cách sử dụng một loạt các API scraping để tăng độ tin cậy.
    Hàm sẽ tự động xoay vòng key nếu gặp lỗi và có logic tìm kiếm nội dung linh hoạt hơn.
    """
    global current_scraping_key_index
    
    # SỬA LỖI: Loại bỏ dấu gạch chéo (/) thừa ở cuối base_url để tránh lỗi URL kép
    target_url = f"{base_url.rstrip('/')}/chapter-{chapter_number}"
    
    # Các từ khóa rác cần loại bỏ khỏi nội dung
    junk_keywords = [
        'novℯls', 'follow current', 'novelupdates', 'wuxiaworld', 'read the latest chapter', 
        'f(r)eewebnov𝒆l', 'this chapt𝙚r is updated by fr(e)ew𝒆bnov(e)l.com', 
        'this chapt𝙚r', 'fr(e)ew𝒆bnov(e)l.com', 'is updated by'
    ]

    # Thử tất cả các API key có trong danh sách
    if not SCRAPING_API_KEYS:
        logging.error("Danh sách SCRAPING_API_KEYS rỗng. Không thể lấy dữ liệu.")
        return None, None

    for i in range(len(SCRAPING_API_KEYS)):
        current_service = SCRAPING_API_KEYS[current_scraping_key_index]
        api_key = current_service["key"]
        service_name = current_service["name"]
        
        logging.info(f"API Scraping: Đang thử lấy chương {chapter_number} bằng {service_name}...")
        
        # Cấu hình params cho từng dịch vụ
        if service_name == "ScraperAPI":
            params = {"api_key": api_key, "url": target_url}
            api_url = "https://api.scraperapi.com/"
        elif service_name == "ScrapingBee":
            params = {"api_key": api_key, "url": target_url, "render_js": "false"}
            api_url = "https://app.scrapingbee.com/api/v1/"
        else:
            logging.warning(f"Dịch vụ scraping '{service_name}' không được hỗ trợ. Bỏ qua.")
            current_scraping_key_index = (current_scraping_key_index + 1) % len(SCRAPING_API_KEYS)
            continue

        try:
            response = requests.get(api_url, params=params, timeout=120)
            
            # Xử lý lỗi và xoay vòng key
            if response.status_code in [401, 403, 429]: # Unauthorized, Forbidden, Too Many Requests
                logging.warning(f"Dịch vụ {service_name} báo lỗi {response.status_code}. Đang chuyển key...")
                current_scraping_key_index = (current_scraping_key_index + 1) % len(SCRAPING_API_KEYS)
                continue
            
            response.raise_for_status() # Ném lỗi cho các status code 4xx/5xx khác
            html_content = response.text
            
            # Phân tích HTML
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # --- LOGIC LẤY TIÊU ĐỀ VÀ NỘI DUNG ĐƯỢC CẢI TIẾN ---

            # 1. Tìm vùng chứa nội dung chính
            content_selectors = ['div.txt', 'div#article', 'div#content', 'div.reading-content', 'div.chapter-content']
            content_element = None
            for selector in content_selectors:
                content_element = soup.select_one(selector)
                if content_element:
                    logging.info(f"Tìm thấy vùng chứa nội dung bằng selector: '{selector}'")
                    break
            
            if not content_element:
                raise ValueError(f"Không tìm thấy thẻ div chứa nội dung chính với các selector đã thử: {content_selectors}")

            # 2. Tìm tiêu đề (logic được cập nhật)
            # Ưu tiên tìm các selector cụ thể ở bất cứ đâu trong trang trước
            title_selectors_specific = ['span.chapter', 'h3.tit']
            # Sau đó tìm các selector chung chung bên trong vùng nội dung
            title_selectors_generic = ['h1', 'h2', 'h3', 'h4', '.chapter-title', '.title']
            
            title_element = None
            
            # Thử selector cụ thể trước trên toàn bộ `soup`
            for selector in title_selectors_specific:
                title_element = soup.select_one(selector)
                if title_element:
                    logging.info(f"Tìm thấy tiêu đề bằng selector cụ thể: '{selector}'")
                    break
            
            # Nếu không thấy, thử selector chung chung bên trong `content_element`
            if not title_element:
                for selector in title_selectors_generic:
                    title_element = content_element.select_one(selector)
                    if title_element:
                        logging.info(f"Tìm thấy tiêu đề bằng selector chung: '{selector}'")
                        break

            title = title_element.get_text(strip=True) if title_element else f"Chương {chapter_number}"
            
            # Nếu tìm thấy thẻ tiêu đề, xóa nó đi để không bị lặp lại trong nội dung
            # Điều này an toàn ngay cả khi nó nằm ngoài content_element
            if title_element:
                 title_element.decompose()

            # 3. Lọc và làm sạch nội dung
            story_lines = []
            for p in content_element.find_all('p'):
                text = p.get_text(strip=True)
                if text and not any(keyword in text.lower() for keyword in junk_keywords):
                    story_lines.append(text)
            
            story_text = '\n\n'.join(story_lines)

            if not story_text:
                raise ValueError("Không tìm thấy nội dung truyện (thẻ <p>) trong HTML sau khi lọc.")
            
            logging.info(f"Lấy thành công chương {chapter_number} bằng {service_name}.")
            return title, story_text

        except requests.exceptions.RequestException as e:
            logging.error(f"Lỗi kết nối với {service_name}: {e}. Đang thử key tiếp theo...")
            current_scraping_key_index = (current_scraping_key_index + 1) % len(SCRAPING_API_KEYS)
            continue
        except ValueError as e:
            logging.error(f"Lỗi phân tích HTML: {e}. Có thể cấu trúc trang web đã thay đổi.")
            current_scraping_key_index = (current_scraping_key_index + 1) % len(SCRAPING_API_KEYS)
            continue

    # Nếu đã thử hết các key mà vẫn lỗi
    logging.error(f"Đã thử hết tất cả API scraping nhưng không lấy được chương {chapter_number} từ URL: {target_url}")
    return None, None
