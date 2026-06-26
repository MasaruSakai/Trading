# Trading Project (Mac Side)

このプロジェクトは、日本株および米国株の自動取引支援、マーケットデータ分析、およびストップロス（逆指値）注文の自動管理を行うためのPythonスクリプト群です。
macOS環境向けに最適化されており、moomoo（Futu OpenD API）および auカブコム証券（Kabu Station API）と連携します。

## 主要スクリプトと役割

### 1. ストップロス（逆指値）注文自動更新
*   **`run_auto_stop_loss.py` (米国株)**
    *   **役割**: moomoo（Futu OpenD API）を介して、保有する米国株ロングポジションの逆指値注文（STOP_LIMIT）を自動的に計算・更新します。
    *   **ロジック**: 保有株の含み損益、現在値、コスト価格、今日のVWAP、過去14日間の Median True Range (MTR) に基づき、最適な逆指値価格を算出します。
    *   **設定**: アカウントID（`ACC_ID`）はスクリプト内の定数 `ACC_ID = 284852706236374484` として指定されています。
*   **`run_kabu_stop_loss.py` (日本株)**
    *   **役割**: Kabu Station API を介して、保有する日本株の逆指値（W指値）注文を自動更新します。株価データの一部は Futu OpenD API も併用します。
    *   **特徴**: 日本の祝日（2026/2027年ハードコード）や年末年始（12/31〜1/3）、土日を考慮して、有効期限（`ExpireDay`）を翌営業日に自動延長します。
    *   **設定**: 接続先URLやパスワードファイル等はコマンドライン引数（`--base-url`, `--password-file`）で指定可能です。

### 2. 注文一括キャンセル
*   **`cancel_all_orders_moomoo.py` (米国株)**
    *   **役割**: moomooで現在発注されているすべてのアクティブな注文（買・売両方、指値・逆指値問わずすべて）を一括でキャンセルします。
    *   **設定**: アカウントID `284852706236374484` に対して動作します。
*   **`cancel_all_orders_kabu.py` (日本株)**
    *   **役割**: Kabu Station API で発注されているすべてのアクティブな未約定注文（`State != 5`）を一括でキャンセルします。

### 3. 分析・シグナル生成
*   **`analysis_enhanced.py`**
    *   **役割**: 日本株・米国株のデータを詳細に分析し、注文フローメトリクス（OBV, OBI, VWAP乖離度）から売買シグナルを生成してプロットします。
*   **`morning_analysis.py`**
    *   **役割**: 日本市場の開始前に、前日の米国市場や主要ETF、ニュースデータ等を元に朝の相場環境予測・要約分析を行います。
*   **`kabu_japan_analysis.py`** / **`japan_analysis.py`**
    *   **役割**: 日本株市場の板情報（Kabu Station API経由）や出来高等のデータを元に、短期的な需給バランスやトレンドを分析します。

### 4. 共通モジュール・クライアント
*   **`stop_order_manager.py`**
    *   moomoo用の逆指値注文の発注・更新処理をカプセル化したモジュールです。`STOP_LIMIT` 注文タイプを使用し、時間外取引（`session=Session.ALL`）および特定口座（`jp_acc_type=SubAccType.JP_TOKUTEI`）向けに発注します。
*   **`kabu_client.py`**
    *   Kabu Station API と通信するための軽量なPythonクライアントです。認証トークンの取得、板情報の取得、注文・キャンセル発注などをサポートします。

---

## 開発と実行環境の設定

### 前提条件
*   macOS環境
*   Python 3.8以上
*   接続環境:
    *   Futu OpenD ゲートウェイがローカル（`127.0.0.1:11111`）で起動していること。
    *   kabuステーション（APIモード）が起動していること（日本株取引用のWindowsプロキシ等に中継される設定を含む）。

### クイックスタート

#### 1. 米国株の自動逆指値更新を実行
```bash
python3 run_auto_stop_loss.py
```

#### 2. 日本株の自動逆指値更新を実行
```bash
python3 run_kabu_stop_loss.py --base-url "http://10.215.1.57:18180" --password-file "kabu_station_server/config/kabu_password.txt"
```

#### 3. 米国株の全アクティブ注文をキャンセル
```bash
python3 cancel_all_orders_moomoo.py
```

#### 4. 日本株の全アクティブ注文をキャンセル
```bash
python3 cancel_all_orders_kabu.py --base-url "http://10.215.1.57:18180" --password-file "kabu_station_server/config/kabu_password.txt"
```
