# プロジェクト引き継ぎノート (Handover Notes)

本ファイルは、ChatGPT/Codex と Antigravity (Gemini) の間でスムーズに開発・分析の文脈を引き継ぐための共有ステータスシートです。

---

## 1. 現在のシステム目標とフェーズ
*   **目的**: 引け前30〜60分に分析を実行し、上昇しやすい日米の株式銘柄候補を大口資金フロー基準（超大口最重視、大口加重、小口逆補正）でスクリーニングする。
*   **直近のフェーズ**:
    *   moomoo OpenAPIを用いた「日米市場分析」「保有銘柄分析」の運用。
    *   Windows上のkabuステーションAPIを通じた日本株分析（現在は仮組みで、一時的にWeb UIからは実行ボタンを非表示化している）。

---

## 2. 直近の変更内容と適用ブランチ
直近で以下の変更を `ag/` 系統のブランチで実装し、`develop` を経て `main` へマージ完了しています。

*   **共通ルール追加 ([AGENTS.md](file:///Users/masaru/Projects/Trading/.agents/AGENTS.md))**:
    *   「後工程はお客様」ルールの追加（各エージェントの能動的な終了/ブロック報告）。
    *   「メイン環境（/Users/masaru/Projects/Trading）での `git checkout` 禁止」ルールの追加。
*   **kabuステーションAPI代替ボタンの非表示化**:
    *   [server.py](file:///Users/masaru/Projects/Trading/server.py) 内の該当HTMLセクションをコメントアウト（PR #13 -> #14）。
*   **iPhone 16e 向けのモバイルUI最適化**:
    *   [server.py](file:///Users/masaru/Projects/Trading/server.py) にモバイル専用のメディアクエリを追加。画面高さを固定（100vh/Flexbox）し、ボタンや余白をスリム化しつつタップ可能領域を確保し、ログ表示preエリアが画面の残り全縦幅を埋めるように改修（PR #15 -> #16）。

---

## 3. 主要な開発・運用ルール（詳細は [AGENTS.md](file:///Users/masaru/Projects/Trading/.agents/AGENTS.md) 参照）
1.  **ブランチの固定**: メインディレクトリ（`/Users/masaru/Projects/Trading`）は常に `main` ブランチに固定します。開発やコミット作業は、エージェントが自動生成する個別ワークツリーまたは一時ワークツリー側で実行し、PR経由で統合します。
2.  **WindowsとMacの分離**: `kabu_station_server` フォルダはWindows側プロキシのコードのみを配置し、共通処理であってもMac側のファイルを持ち込まない。
3.  **後工程はお客様**: すべてのエージェントはタスク終了またはブロック時に黙って待機せず、必ず呼び出し元に明確な報告・質問を行う。

---

## 4. 次のアクション候補（TODO）
*   [ ] ユーザー様とのトレード戦略・仮説に関する相談（必要に応じて `strategy_analyst` を使ったバックテスト検証の実施）。
*   [ ] kabuステーションAPI連携プロキシ（Windows側）の機能開発の再開時における、Web UIボタンの再表示および繋ぎ込み。
