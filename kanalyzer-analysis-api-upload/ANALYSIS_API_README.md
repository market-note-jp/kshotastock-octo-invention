# KAnalyzer Analysis API

公開サイト版からEDINET検索とXBRL財務分析を実行するためのPython APIです。

## ローカル起動

```powershell
cd edinet_financial_app
$env:EDINET_API_KEY="EDINETで取得したAPIキー"
python -m uvicorn analysis_api:app --host 127.0.0.1 --port 8000
```

確認:

```powershell
curl http://127.0.0.1:8000/health
```

## 公開サイトとの接続

サイト側の環境変数 `ANALYSIS_API_BASE_URL` に、このAPIの公開URLを設定します。

例:

```text
ANALYSIS_API_BASE_URL=https://kanalyzer-analysis-api.example.com
```

EDINET APIキーはPython API側の `EDINET_API_KEY` にだけ設定します。サイト側やブラウザには置きません。

## Renderで使う場合

`render.analysis-api.yaml` を使うと、DockerベースのWeb Serviceとして起動できます。Render側で `EDINET_API_KEY` をSecretとして設定してください。

