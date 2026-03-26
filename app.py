import os
from dotenv import load_dotenv
import time
import requests
import threading
from bs4 import BeautifulSoup
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, g, render_template, redirect, url_for
from flask_cors import CORS
import google.generativeai as genai

import firebase_admin
from firebase_admin import credentials, firestore, auth

import uuid
import google.generativeai as genai

import random

from datetime import datetime, time, timezone

load_dotenv()

try:
    # firebaseへの接続
    cred_path = os.environ.get('FIREBASE_ADMINSDK_JSON_PATH')
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebaseとの接続に成功しました。")
except Exception as e:
    print(f"❌ Firebaseの初期化中にエラーが発生しました: {e}")
    db = None

app = Flask(__name__)
CORS(app)

try:
    # Gemini APIの使用
    API_KEY = os.environ.get('GEMINI_API_KEY')
    if not API_KEY or "YOUR_GEMINI_API_KEY" in API_KEY:
        print("⚠️ 警告: Gemini APIキーが.envファイルに設定されていません。")
    genai.configure(api_key=API_KEY)
    model_name = 'gemini-2.5-flash-lite'
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            if 'flash' in m.name:
                model_name = m.name
                break
    
    model = genai.GenerativeModel(model_name)
    print(f"✅ Geminiモデル ({model_name}) の準備ができました。")
except Exception as e:
    print(f"❌ APIキーの設定中にエラーが発生しました: {e}")
    model = None

# web用のログインしていないユーザーを弾く
def login_required_for_web(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        id_token = request.cookies.get('firebaseToken')
        if not id_token:
            return redirect(url_for('login_page'))
        try:
            decoded_token = auth.verify_id_token(id_token)
            g.user_id = decoded_token['uid']
            g.user = auth.get_user(decoded_token['uid'])
        except Exception as e:
            print(f"Web token verification failed: {e}")
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

# API用のログインしていないユーザーを弾く
def login_required_for_api(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({"error": "Authorization header is missing or invalid"}), 401
        id_token = auth_header.split('Bearer ')[1]
        try:
            decoded_token = auth.verify_id_token(id_token)
            g.user_id = decoded_token['uid']
        except Exception as e:
            print(f"API token verification failed: {e}")
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated_function

# 第一のキーワードによるフィルタリング
def is_it_tech(title, url):
    keywords = [
        '技術', 'IT', 'プログラミング', 'エンジニア', '開発', 'API', 'AI', '人工知能', 'Python', 'JavaScript', 
        'プログラム', 'システム', 'ソフトウェア', 'ハードウェア', 'クラウド', 'サーバ', 'データ', 'ネットワーク', 'セキュリティ', 'Web技術', 
        'IT技術', 'Google', 'Chrome', 'Takeout', 'GitHub', 'コード','API', 'AI', 'ML', '機械学習', 
        'Deep Learning', 'チュートリアル', '基礎', 'HTML', 'CSS', 'React', 'フロントエンド', 'バックエンド'
    ]
    title = title or ""
    url = url or ""
    for keyword in keywords:
        if keyword.lower() in title.lower() or keyword.lower() in url.lower():
            return True
    return False

# フィルタリングの際に除外したいキーワード
def is_info_page(title, url):
    exclude_keywords = [
        'ホーム', 'トップ', 'home', 'drive', 'mail', 'inbox', 'login', 'signin', 'sign in', '検索結果',
        'Google cloud', 'Google 検索', 'google_vignette',
        'twitter.com', 'x.com', 'facebook.com', 'youtube.com', 'youtu.be', 'instagram.com',
        'amazon.co.jp', 'amazon.com', 'rakuten.co.jp', 'finance.yahoo.co.jp',
        'ツイッター', 'フェイスブック', 'ユーチューブ', 'アマゾン', '楽天', 'ヤフーファイナンス'
    ]
    title = title or ""
    url = url or ""
    for kw in exclude_keywords:
        if kw.lower() in title.lower() or kw.lower() in url.lower():
            return False
    if '.google.com' in url and not 'developers.google.com' in url and not 'cloud.google.com/blog' in url:
        return False
    return True

# スクレイピング処理
def scrape_content(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        ogp_data = {}
        for meta in soup.find_all('meta'):
            prop = meta.get('property')
            if prop and prop.startswith('og:'):
                key = prop.split(':')[1]
                ogp_data[key] = meta.get('content')

        for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form']):
            element.decompose()
        text = ' '.join(t.strip() for t in soup.get_text().split())
        return {'text': text[:2000], 'ogp': ogp_data}
    except Exception as e:
        print(f"スクレイピングエラー ({url}): {e}")
        return None

# 第二のGeminiによるフィルタリング
def classify_content(text_snippet):
    if not model:
        print("エラー: Geminiモデルが初期化されていません。")
        return False
    prompt = f"""
        以下の文章は「IT技術解説の記事」か「IT無関係の記事」かを分類してください。
        文章: {text_snippet}
        「IT技術解説」の場合は technical、「IT無関係」の場合は none とだけ答えてください。
        情報サイトのTOP場合もnoneと答えてください。
    """
    try:
        response = model.generate_content(prompt)
        answer = response.text.strip().lower()
        return 'technical' in answer
    except Exception as e:
        print(f"Geminiでの分類失敗: {e}")
        return False

# Geminiによる要約やタグ付の処理
def process_and_summarize_entry(entry):
    """
    単一の履歴エントリに対して、取得・分類・要約までを一貫して行う関数。
    ThreadPoolExecutorによって並列で実行される。
    """
    title = entry.get('title', '')
    url = entry.get('url', '')

    if not (url and url.startswith('http') and is_it_tech(title, url) and is_info_page(title, url)):
        return None

    scrape_result = scrape_content(url)
    if not scrape_result:
        return None
    
    content = scrape_result['text']
    ogp_data = scrape_result['ogp']
    if not content or len(content) < 100:
        return None

    if not classify_content(content[:1000]):
        print(f"  -> ❌ 技術記事ではないと判断: {title}")
        return None
    
    print(f"  -> ✅ 技術記事として分類: {title}")

    if not model:
        return None
    try:
        prompt = f"""
            以下のWebサイトの内容を要約してください:
            タイトル: {title}
            URL: {url}
            コンテンツ: {content[:1500]}...
            必ず下記のフォーマットに従って記述してください。
            タイトル:（ここにAIが生成した、内容が分かりやすいタイトルを記載）
            情報元:（ここにURLではなく、Webサイト名やサービス名を記載）
            要約:（サイト内容をIT技術の観点から一言で要約してください。タグ用語を必ず含めて、である調で記述してください。太字などはなしでシンプルなテキストで書いてください。）
            タグ:（重要：必ず下記の「タグリスト」の中から、内容に最も関連する単語を2つだけ選んでください。リストにない単語は絶対に使用しないでください。）
            タグリスト: サーバー, ネットワーク,HTML, CSS, JavaScript, Java, Python, PHP, Ruby, Rust, swift, C言語, C#, C++, TypeScript, Go, サーバーレス, データベース, LLM, Linux, Windows, MacOS, OS,クラウド, AWS, Azure, GCP, Docker, フレームワーク, ライブラリ, API, JSON, SQL, NoSQL, Git, Github, AI, UI/UX, Cloud, ネットワーク, セキュリティ, フロントエンド, バックエンド, Web3
        """
        response = model.generate_content(prompt)
        
        lines = response.text.strip().split('\n')
        article_data = {'originalUrl': url, 'originalTitle': title}
        for line in lines:
            if ':' in line:
                key, value = [x.strip() for x in line.split(':', 1)]
                if key == 'タイトル': article_data['generatedTitle'] = value
                elif key == '情報元': article_data['source'] = value
                elif key == '要約': article_data['summary'] = value
                elif key == 'タグ': article_data['tags'] = [tag.strip() for tag in value.split(',')]

        article_data['ogp'] = ogp_data
        if all(k in article_data for k in ['generatedTitle', 'summary', 'tags']):
            print(f"  -> 📝 要約生成成功: {article_data.get('generatedTitle')}")
            return article_data
        else:
            print(f"  -> ⚠️ 要約結果の形式が不正: {title}")
            return None

    except Exception as e:
        print(f"  -> 🚨 Geminiでの要約中にエラー: {e}")
        return None

# 重複したURLの除外と並列処理、データベースへの保存
def process_and_summarize_history(history_data, user_id, job_id):
    print(f"\n--- 履歴の処理を開始します (User: {user_id}, Job: {job_id}) ---")
    
    candidate_urls = list(set(entry.get('url') for entry in history_data if entry.get('url')))
    if not candidate_urls:
        print("処理対象のURLがありません。")
        job_ref = db.collection('users').document(user_id).collection('jobs').document(job_id)
        job_ref.update({'status': 'complete', 'completedAt': firestore.SERVER_TIMESTAMP})
        return

    print(f"履歴から {len(candidate_urls)} 件のユニークなURLを抽出しました。")
    
    now = datetime.now(timezone.utc)
    start_of_today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)

    urls_already_processed_today = set()
    chunk_size = 30
    for i in range(0, len(candidate_urls), chunk_size):
        chunk = candidate_urls[i:i + chunk_size]
        try:
            query = db.collection('users').document(user_id).collection('articles') \
                      .where('originalUrl', 'in', chunk) \
                      .where('createdAt', '>=', start_of_today)
            
            docs = query.stream()
            for doc in docs:
                urls_already_processed_today.add(doc.to_dict().get('originalUrl'))
        except Exception as e:
            print(f"URLの存在確認中にエラー: {e}")

    if urls_already_processed_today:
        print(f"今日既に保存済みのURLが {len(urls_already_processed_today)} 件見つかりました。これらはスキップされます。")

    entries_to_process = [entry for entry in history_data if entry.get('url') not in urls_already_processed_today]
    
    print(f"新規処理対象の記事は {len(entries_to_process)} 件です。並列処理を開始します。")
    if not entries_to_process:
        print("新規処理対象の記事はありませんでした。処理を終了します。")
        job_ref = db.collection('users').document(user_id).collection('jobs').document(job_id)
        job_ref.update({'status': 'complete', 'completedAt': firestore.SERVER_TIMESTAMP, 'newArticleIds': []})
        return

    summarized_articles = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_entry = {executor.submit(process_and_summarize_entry, entry): entry for entry in entries_to_process}
        for future in as_completed(future_to_entry):
            result = future.result()
            if result:
                summarized_articles.append(result)

    new_article_ids = []
    if summarized_articles:
        print(f"\n{len(summarized_articles)}件の記事の要約が完了しました。データベースに一括保存します。")
        try:
            user_articles_ref = db.collection('users').document(user_id).collection('articles')
            batch = db.batch()
            for article_data in summarized_articles:
                article_data['createdAt'] = firestore.SERVER_TIMESTAMP
                doc_ref = user_articles_ref.document()
                batch.set(doc_ref, article_data)
                new_article_ids.append(doc_ref.id)
            batch.commit()
            print(f"✅ {len(new_article_ids)}件の記事をFirestoreに保存しました。")
        except Exception as e:
            print(f"❌ Firestoreへのバッチ保存中にエラー: {e}")
    else:
        print("要約対象の記事は見つかりませんでした。")
    
    try:
        job_ref = db.collection('users').document(user_id).collection('jobs').document(job_id)
        job_ref.update({
            'status': 'complete',
            'newArticleIds': new_article_ids,
            'completedAt': firestore.SERVER_TIMESTAMP
        })
        print(f"✅ ジョブが完了しました (Job: {job_id})。新規記事: {len(new_article_ids)}件")
    except Exception as e:
        print(f"❌ ジョブの更新中にエラー: {e}")
    print("\n--- 全ての処理が完了しました ---")


@app.route('/')
def index():
    id_token = request.cookies.get('firebaseToken')
    if id_token:
        try:
            auth.verify_id_token(id_token)
            return redirect(url_for('dashboard'))
        except:
            return render_template('login.html')
    return render_template('login.html')

# @app.route('/select')
# @login_required_for_web
# def select_view():
#     user_id = g.user_id
#     try:
#         articles_ref = db.collection('users').document(user_id).collection('articles').stream()
#         all_tags = set()
#         for doc in articles_ref:
#             article_data = doc.to_dict()
#             if 'tags' in article_data and article_data['tags']:
#                 for tag in article_data['tags']:
#                     all_tags.add(tag)
#         sorted_tags = sorted(list(all_tags))
#         return render_template('select_view.html', tags=sorted_tags, user_email=g.user.email)
#     except Exception as e:
#         print(f"❌ タグ一覧の取得エラー: {e}")
#         return "タグの取得中にエラーが発生しました。", 500


@app.route('/dashboard')
@login_required_for_web
def dashboard():
    user_id = g.user_id
    tag_filter = request.args.get('filter')
    keyword_query = request.args.get('q')
    search_type = request.args.get('search_type', 'all')

    try:
        recommended_articles = []
        recommended_ids = []
        recommendation_ref = db.collection('users').document(user_id).collection('recommendations').document('weekly')
        recommendation_doc = recommendation_ref.get()

        if recommendation_doc.exists:
            recommended_ids = recommendation_doc.to_dict().get('articleIds', [])
            if recommended_ids:
                articles_ref_for_rec = db.collection('users').document(user_id).collection('articles')
                for article_id in recommended_ids:
                    doc = articles_ref_for_rec.document(article_id).get()
                    if doc.exists:
                        article_data = doc.to_dict()
                        article_data['id'] = doc.id
                        if 'createdAt' in article_data and article_data['createdAt']:
                            article_data['formatted_date'] = article_data['createdAt'].strftime('%Y-%m-%d')
                        recommended_articles.append(article_data)

        articles_ref = db.collection('users').document(user_id).collection('articles')
        
        if tag_filter:
            if tag_filter == 'readLater':
                query = articles_ref.where('readLater', '==', True).order_by('createdAt', direction=firestore.Query.DESCENDING)
            else:
                query = articles_ref.where('tags', 'array_contains', tag_filter).order_by('createdAt', direction=firestore.Query.DESCENDING)
        else:
            query = articles_ref.order_by('createdAt', direction=firestore.Query.DESCENDING)
        
        docs = list(query.stream())
        
        url_counts = {}
        for doc in docs:
            url = doc.to_dict().get('originalUrl')
            if url:
                url_counts[url] = url_counts.get(url, 0) + 1

        articles = []
        seen_url_counter = {}

        for doc in docs:
            article_data = doc.to_dict()
            article_id = doc.id
            url = article_data.get('originalUrl')
            total_visits = url_counts.get(url, 0)
            already_seen_count = seen_url_counter.get(url, 0)
            
            visit_number = total_visits - already_seen_count
            seen_url_counter[url] = already_seen_count + 1

            article_data['visit_number'] = visit_number
            article_data['is_repeat'] = total_visits > 1
            article_data['id'] = article_id

            if keyword_query:
                keyword_lower = keyword_query.lower()
                is_match = False

                gen_title = article_data.get('generatedTitle', '').lower()
                orig_title = article_data.get('originalUrl', '').lower() 
                summary = article_data.get('summary', '').lower()
                tags = [t.lower() for t in article_data.get('tags', [])]

                reflection_text = ""
                if 'reflection' in article_data and isinstance(article_data['reflection'], dict):
                    r = article_data['reflection']
                    reflection_text += r.get('specific_impression', '').lower()
                    reflection_text += r.get('why_important', '').lower()
                    reflection_text += r.get('what_i_got', '').lower()
                    reflection_text += r.get('memo', '').lower()

                if search_type == 'tag':
                    is_match = any(keyword_lower in tag for tag in tags)
                elif search_type == 'title':
                    is_match = (keyword_lower in gen_title)
                elif search_type == 'reflection':
                    is_match = (keyword_lower in reflection_text)
                else:
                    search_corpus = gen_title + summary + reflection_text
                    is_match = (keyword_lower in search_corpus) or any(keyword_lower in tag for tag in tags)

                if not is_match:
                    continue

            if 'createdAt' in article_data and article_data['createdAt']:
                article_data['formatted_date'] = article_data['createdAt'].strftime('%Y-%m-%d')
            else:
                article_data['formatted_date'] = '日付なし'
            
            articles.append(article_data)

        return render_template('dashboard.html', 
                               user_email=g.user.email, 
                               articles=articles,
                               recommended_articles=recommended_articles,
                               current_filter=tag_filter,
                               keyword_query=keyword_query,
                               search_type=search_type)

    except Exception as e:
        print(f"❌ データ取得エラー: {e}")
        if 'index' in str(e):
            error_message = """
            <h1>データベース設定エラー</h1>
            <p>Firestoreの「複合インデックス」が不足している可能性があります。ログを確認してください。</p>
            """
            return error_message, 500
        return "データの取得中にエラーが発生しました。", 500
    
@app.route('/article/<article_id>')
@login_required_for_web
def article_detail(article_id):
    user_id = g.user_id
    try:
        doc_ref = db.collection('users').document(user_id).collection('articles').document(article_id)
        doc = doc_ref.get()
        if doc.exists:
            article_data = doc.to_dict()
            article_data['id'] = doc.id
            
            return render_template('article_detail.html', article=article_data, user_email=g.user.email)
        else:
            return "記事が見つかりません。", 404
    except Exception as e:
        print(f"❌ 記事詳細の取得エラー: {e}")
        return "記事の取得中にエラーが発生しました。", 500
    
@app.route('/article/<article_id>', methods=['DELETE'])
@login_required_for_api
def delete_article(article_id):
    """
    指定されたIDの記事をデータベースから削除するAPIエンドポイント。
    """
    user_id = g.user_id
    try:
        doc_ref = db.collection('users').document(user_id).collection('articles').document(article_id)

        if not doc_ref.get().exists:
            return jsonify({"error": "Article not found"}), 404

        doc_ref.delete()
        print(f"✅ 記事を削除しました (User: {user_id}, Article: {article_id})")
        return jsonify({"status": "success", "message": "Article deleted successfully"}), 200
    except Exception as e:
        print(f"❌ 記事の削除中にエラー: {e}")
        return jsonify({"error": "Failed to delete article"}), 500

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/history', methods=['POST'])
@login_required_for_api
def receive_history():
    history_data = request.get_json()
    if not isinstance(history_data, list):
        return jsonify({"error": "Invalid JSON"}), 400
    
    user_id = g.user_id
    job_id = str(uuid.uuid4())
    try:
        job_ref = db.collection('users').document(user_id).collection('jobs').document(job_id)
        job_ref.set({
            'status': 'processing',
            'createdAt': firestore.SERVER_TIMESTAMP,
            'newArticleIds': []
        })
        print(f"➡️  認証済みユーザー ({user_id}) から {len(history_data)}件の履歴を受信。Job ID: {job_id}")
        
        thread = threading.Thread(target=process_and_summarize_history, args=(history_data, user_id, job_id))
        thread.start()
        
        return jsonify({"status": "processing_started", "job_id": job_id}), 202
    except Exception as e:
        print(f"❌ ジョブの作成中にエラー: {e}")
        return jsonify({"error": "Failed to create a processing job"}), 500

@app.route('/create_user_profile', methods=['POST'])
@login_required_for_api
def create_user_profile():
    user_id = g.user_id
    data = request.get_json()
    email = data.get('email')
    if not email:
        return jsonify({"error": "Email is required"}), 400
    try:
        user_ref = db.collection('users').document(user_id)
        if not user_ref.get().exists:
            user_ref.set({
                'email': email,
                'createdAt': firestore.SERVER_TIMESTAMP
            })
            print(f"✅ 新規ユーザープロファイルを作成しました (User: {user_id})")
        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"❌ ユーザープロファイル作成中にエラー: {e}")
        return jsonify({"error": "Failed to create user profile"}), 500

@app.route('/article/<article_id>/reflection', methods=['POST'])
@login_required_for_api
def save_reflection(article_id):
    user_id = g.user_id
    data = request.get_json()
    reflection_data = {
        'usefulness' : data.get('usefulness'),
        'impression' : data.get('impression'),
        'specific_impression' : data.get('specific_impression'),
        'why_important' : data.get('why_important'),
        'content_type' : data.get('content_type'),
        'what_i_got' : data.get('what_i_got'),
        'memo' : data.get('memo')
    }
    required_keys = ['usefulness', 'impression', 'content_type', 'specific_impression', 'why_important', 'what_i_got', 'memo']
    if not all(key in data for key in required_keys):
        return jsonify({"error": "Missing reflection data"}), 400
    
    try:
        doc_ref = db.collection('users').document(user_id).collection('articles').document(article_id)
        doc_ref.update({
            'reflection': reflection_data,
            'updatedAt': firestore.SERVER_TIMESTAMP
        })
        print(f"✅ 振り返りを保存しました (User: {user_id}, Article: {article_id})")
        return jsonify({"status": "success", "message": "Reflection saved successfully"}), 200
    except Exception as e:
        print(f"❌ 振り返りの保存中にエラー: {e}")
        return jsonify({"error": "Failed to save reflection"}), 500

@app.route('/processing/<job_id>')
@login_required_for_web
def processing_page(job_id):
    return render_template('processing.html', job_id=job_id, user_email=g.user.email)

@app.route('/reflect')
@login_required_for_web
def reflect_page():
    user_id = g.user_id
    ids_str = request.args.get('ids', '')
    index_str = request.args.get('index', '0')
    if not ids_str:
        return redirect(url_for('dashboard'))
    ids = ids_str.split(',')
    index = int(index_str)
    if index >= len(ids):
        return redirect(url_for('dashboard'))
    current_article_id = ids[index]
    try:
        doc_ref = db.collection('users').document(user_id).collection('articles').document(current_article_id)
        max_retries = 3
        doc = None
        for i in range(max_retries):
            doc = doc_ref.get()
            if doc.exists:
                break
            print(f"記事取得リトライ中... ({i + 1}/{max_retries}) ID: {current_article_id}")
            time.sleep(0.5)
        if doc and doc.exists:
            article = doc.to_dict()
            progress = f"{index + 1}/{len(ids)}"
            return render_template('reflect.html', 
                                   article=article, 
                                   article_id=current_article_id,
                                   all_ids=ids_str,
                                   current_index=index,
                                   progress=progress,
                                   user_email=g.user.email)
        else:
            print(f"⚠️ 記事が見つかりませんでした (ID: {current_article_id})。スキップします。")
            next_index = index + 1
            if next_index >= len(ids):
                return redirect(url_for('dashboard'))
            return redirect(url_for('reflect_page', ids=ids_str, index=next_index))
    except Exception as e:
        print(f"❌ 振り返り記事の取得エラー: {e}")
        return "記事の取得中にエラーが発生しました。", 500



@app.route('/api/generate-recommendations', methods=['POST'])
@login_required_for_api
def generate_recommendations():
    """
    【自動実行用API】S/Aティア、または「後で見る」が設定された記事からランダムで3つ選び、おすすめとして保存する。
    """
    user_id = g.user_id
    print(f"週次レコメンド生成を開始します (User: {user_id})")
    
    try:
        articles_ref = db.collection('users').document(user_id).collection('articles')
        
        query_s = articles_ref.where('reflection.usefulness', '==', 'tier-s').stream()
        query_a = articles_ref.where('reflection.usefulness', '==', 'tier-a').stream()
        
        query_read_later = articles_ref.where('readLater', '==', True).stream()

        candidate_articles = {}
        for doc in query_s:
            candidate_articles[doc.id] = doc
        for doc in query_a:
            candidate_articles[doc.id] = doc
        for doc in query_read_later:
            candidate_articles[doc.id] = doc

        high_value_articles = list(candidate_articles.values())

        if len(high_value_articles) < 3:
            print(f"  -> おすすめ対象の記事が3件未満のため、処理をスキップしました。")
            return jsonify({"status": "skipped", "reason": "Not enough high-value articles"}), 200

        recommended_docs = random.sample(high_value_articles, 3)
        recommended_ids = [doc.id for doc in recommended_docs]
        
        recommendation_ref = db.collection('users').document(user_id).collection('recommendations').document('weekly')
        recommendation_ref.set({
            'articleIds': recommended_ids,
            'createdAt': firestore.SERVER_TIMESTAMP
        })
        
        print(f"✅ おすすめ記事を3件保存しました (User: {user_id})")
        return jsonify({"status": "success", "recommended_ids": recommended_ids}), 200

    except Exception as e:
        print(f"❌ おすすめ生成中にエラー: {e}")
        return jsonify({"error": "Failed to generate recommendations"}), 500

@app.route('/api/article/<article_id>/read_later', methods=['POST'])
@login_required_for_api
def toggle_read_later(article_id):
    """
    記事の「後で見る」状態を切り替えるAPI。
    """
    user_id = g.user_id
    try:
        doc_ref = db.collection('users').document(user_id).collection('articles').document(article_id)
        doc = doc_ref.get()

        if not doc.exists:
            return jsonify({"error": "Article not found"}), 404

        current_status = doc.to_dict().get('readLater', False)
        new_status = not current_status
        
        doc_ref.update({'readLater': new_status})
        
        return jsonify({"status": "success", "readLater": new_status}), 200

    except Exception as e:
        print(f"❌「後で見る」状態の変更中にエラー: {e}")
        return jsonify({"error": "Failed to update status"}), 500
    
@app.route('/privacy')
def privacy_policy_page():
    """
    プライバシーポリシーを表示するページ。
    このページはログイン不要でアクセス可能です。
    """
    return render_template('privacy.html')
    
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    
    debug_mode = os.environ.get('FLASK_DEBUG') == '1'
    
    app.run(host='0.0.0.0', port=port, debug=debug_mode)