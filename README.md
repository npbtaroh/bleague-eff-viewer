# B.LEAGUE EFF Viewer

試合日・リーグ別のEFFランキングを表示する公開用サイトです。画面はGitHub Pagesで配信し、GitHub ActionsがPythonで試合データを取得してJSONを生成します。

## 公開されるもの

- Pythonの取得・集計コード
- HTML / CSS / JavaScriptの画面
- Actionsが生成し、Pagesに公開するランキングJSON

SQLiteデータベース、ログ、仮想環境はGitHubへ保存しません。Actionsの実行環境でSQLiteを一時作成し、静的JSONだけを公開します。リポジトリがPublicの場合、ソースコードと公開JSONは誰でも閲覧できます。

## 初回の設定

1. このフォルダの中身を `npbtaroh/bleague-eff-viewer` リポジトリの `main` ブランチへ追加します。
2. GitHubのリポジトリで **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。
3. **Actions** タブで `Update B.LEAGUE data and publish` が動き終わるのを待ちます。
4. 公開先は `https://npbtaroh.github.io/bleague-eff-viewer/` です。

`main` への変更時にも更新と公開が走ります。手動更新は **Actions → Update B.LEAGUE data and publish → Run workflow** から実行できます。毎朝6時17分（日本時間）にも自動実行します。GitHub側の混雑で開始が遅れる場合があります。

## データ更新について

Actionsの実行環境は毎回新しく、前回のSQLiteを保持しません。そのため、現在の初期構成では毎回2026年9月21日以降の試合を再確認してDBを組み立て、公式シーズン集計と照合した後、JSONを書き出します。シーズンが長くなると更新時間は延びます。DBやデータファイルを公開リポジトリへ置かずに済みますが、運用後に時間が気になる場合は、保存先を別途用意して差分更新へ切り替えます。

取得元の公式サイトで一時的なエラーが起きると、Actionsは失敗し、新しいページを公開しません。Actionsの実行履歴で失敗を確認できます。

## 手元で実行

Python 3.12以降を用意し、必要なライブラリを入れてから以下を実行します。

```sh
python -m pip install -r requirements.txt
python parse_and_save.py --daily
python tools/build_static_data.py --db bleague_data.db --out site
```

ローカルで画面を確認するときは、生成した `site` フォルダをHTTPサーバーで開いてください。`index.html`を直接開くと、ブラウザの制約でJSONを読めない場合があります。
