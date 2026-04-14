import os
import requests
import re
import time
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from functools import wraps

# 获取当前目录的绝对路径
current_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(current_dir, 'static')

app = Flask(__name__, static_folder=static_dir, static_url_path='/static')
CORS(app)

# ---------- 配置 ----------
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
})

# 如果需要代理，取消注释并修改地址
# SESSION.proxies = {
#     'http': 'http://127.0.0.1:7890',
#     'https': 'http://127.0.0.1:7890'
# }

# 简单缓存（30分钟）
CACHE = {}
CACHE_TTL = 1800

def cache_get(key):
    data = CACHE.get(key)
    if data and time.time() < data['expire']:
        return data['value']
    return None

def cache_set(key, value):
    CACHE[key] = {'value': value, 'expire': time.time() + CACHE_TTL}

# ---------- 爬取特卖列表 ----------
def fetch_special_items(limit=50):
    """返回包含 app_id, name, icon 的基础列表"""
    url = 'https://store.steampowered.com/search/'
    params = {
        'specials': '1',
        'count': limit,
        'sort_by': 'Released_DESC',
        'sort_dir': 'desc',
    }
    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[错误] 获取搜索页失败: {e}")
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')
    rows = soup.select('a.search_result_row')
    items = []
    for row in rows:
        app_id = row.get('data-ds-appid')
        if not app_id:
            continue
        name_tag = row.select_one('.title')
        name = name_tag.text.strip() if name_tag else 'Unknown'
        img_tag = row.select_one('img')
        icon = img_tag.get('src') if img_tag else ''
        items.append({
            'app_id': app_id,
            'name': name,
            'icon': icon,
        })
    print(f"[信息] 从 HTML 提取到 {len(items)} 个游戏 ID")
    return items

# ---------- 批量获取价格和折扣 ----------
def fetch_prices_batch(app_ids):
    """通过 appdetails API 批量获取价格信息"""
    if not app_ids:
        return {}
    # 分批，每批最多 50 个
    batch_size = 50
    results = {}
    for i in range(0, len(app_ids), batch_size):
        batch = app_ids[i:i+batch_size]
        url = 'https://store.steampowered.com/api/appdetails'
        params = {
            'appids': ','.join(batch),
            'filters': 'price_overview',
            'cc': 'cn',          # 中国区价格
            'l': 'schinese'
        }
        try:
            resp = SESSION.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for app_id in batch:
                if data.get(app_id, {}).get('success'):
                    price_info = data[app_id]['data'].get('price_overview')
                    if price_info:
                        results[app_id] = {
                            'price_original': price_info.get('initial_formatted'),
                            'price_final': price_info.get('final_formatted'),
                            'discount_percent': price_info.get('discount_percent', 0)
                        }
                    else:
                        # 可能是免费游戏
                        results[app_id] = {
                            'price_original': None,
                            'price_final': 'Free',
                            'discount_percent': 0
                        }
                else:
                    results[app_id] = None
        except Exception as e:
            print(f"[错误] 批量获取价格失败 (批次 {i//batch_size+1}): {e}")
            # 失败时该批次所有游戏标记为无价格
            for app_id in batch:
                results[app_id] = None
        time.sleep(0.2)  # 避免请求过快
    return results

# ---------- 获取单个游戏详情（用于详情页）----------
def fetch_game_detail(app_id):
    """返回完整详情信息"""
    url = 'https://store.steampowered.com/api/appdetails'
    params = {
        'appids': app_id,
        'filters': 'basic,price_overview',
        'cc': 'cn',
        'l': 'schinese'
    }
    try:
        resp = SESSION.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get(app_id, {}).get('success'):
            return None
        game_data = data[app_id]['data']
        price = game_data.get('price_overview', {})
        result = {
            'app_id': app_id,
            'name': game_data.get('name'),
            'description': game_data.get('short_description', '暂无描述'),
            'developers': game_data.get('developers', []),
            'publishers': game_data.get('publishers', []),
            'release_date': game_data.get('release_date', {}).get('date'),
            'price_original': price.get('initial_formatted'),
            'price_final': price.get('final_formatted'),
            'discount_percent': price.get('discount_percent', 0),
            'header_image': game_data.get('header_image', ''),
            'genres': [g['description'] for g in game_data.get('genres', [])],
            'is_free': game_data.get('is_free', False),
        }
        return result
    except Exception as e:
        print(f"[错误] 获取游戏 {app_id} 详情失败: {e}")
        return None

# ---------- API 路由 ----------
@app.route('/')
def index():
    return app.send_static_file('demo1.html')

@app.route('/demo1.html')
def demo1():
    return app.send_static_file('demo1.html')

@app.route('/api/specials', methods=['GET'])
def get_specials():
    """返回特卖游戏列表（图标、名称、价格、折扣）"""
    per_page = min(int(request.args.get('per_page', 30)), 100)
    cache_key = f'specials_{per_page}'
    cached = cache_get(cache_key)
    if cached:
        return jsonify({'code': 200, 'data': cached})

    # 1. 获取基础列表（app_id, name, icon）
    items = fetch_special_items(limit=per_page)
    if not items:
        return jsonify({'code': 404, 'message': '未获取到游戏列表', 'data': []})

    app_ids = [item['app_id'] for item in items]
    # 2. 批量获取价格
    prices = fetch_prices_batch(app_ids)

    # 3. 组装最终数据
    games = []
    for item in items:
        app_id = item['app_id']
        price_info = prices.get(app_id)
        if price_info is None:
            # 没有价格信息也返回，但价格字段为 None
            price_info = {'price_original': None, 'price_final': 'N/A', 'discount_percent': 0}
        games.append({
            'app_id': app_id,
            'name': item['name'],
            'icon': item['icon'],
            'price_original': price_info['price_original'],
            'price_final': price_info['price_final'],
            'discount_percent': price_info['discount_percent']
        })

    cache_set(cache_key, games)
    return jsonify({'code': 200, 'data': games})

@app.route('/api/game/<app_id>', methods=['GET'])
def get_game_detail(app_id):
    """获取单个游戏的详细信息"""
    cache_key = f'detail_{app_id}'
    cached = cache_get(cache_key)
    if cached:
        return jsonify({'code': 200, 'data': cached})

    detail = fetch_game_detail(app_id)
    if not detail:
        return jsonify({'code': 404, 'message': '游戏不存在'})

    cache_set(cache_key, detail)
    return jsonify({'code': 200, 'data': detail})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)