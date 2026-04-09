# x-article-reader

`x-article-reader` 是一個給 Codex / Claude Code 用的 skill repo，專門處理「看起來是 X status，其實裡面包著長文章」這種情境。

它的重點不是把整個 `x.com` 頁面硬塞給模型，而是先走公開可讀的輕量資料，只有真的需要全文時才打開頁面抓正文。這樣比較省 token，也比較穩。

## 這個 skill 做什麼

它提供 3 種結果層級：

1. 輕量預覽  
只拿文章標題、摘要、原始貼文資訊。

2. 全文提取  
從 status 頁面把長文章正文抓出來，輸出成 Markdown 或 JSON。

3. 失敗退路  
如果正文提取失敗，不會整個報廢，會退回摘要模式，至少保留可讀內容。

## 為什麼不用 X 官方 API

這個 skill 不依賴官方 API key。  
status 外殼的文章資訊會先透過這條公開端點讀取：

```text
https://cdn.syndication.twimg.com/tweet-result?id=<tweet_id>&token=x
```

這一步可以先拿到：
- 貼文基本資料
- 展開後連結
- 文章標題
- 文章摘要

## 需求

目前實測環境需要：

- Python 3
- `browser_use` Python 套件
- `playwright` Python 套件
- 可用的 Chromium / Playwright 瀏覽器環境

## 用法

### Windows

全文提取：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/x_article_reader.ps1 "https://x.com/geoffintech/status/2042002590758572377" --json
```

只跑便宜的摘要退路：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/x_article_reader.ps1 "https://x.com/geoffintech/status/2042002590758572377" --json --no-browser
```

直接讀 author/article 連結：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/x_article_reader.ps1 "https://x.com/rohit4verse/article/2041548810804211936" --json
```

輸出成檔案：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/x_article_reader.ps1 "https://x.com/geoffintech/status/2042002590758572377" --out ".\geoff.md"
```

### macOS / Linux

```bash
bash scripts/x_article_reader.sh "https://x.com/geoffintech/status/2042002590758572377" --json
```

## Harness

這個 repo 內建 live harness，可以用來驗證 3 條路線：

- status 全文提取
- preview fallback
- direct article 提取

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_harness.ps1
```

macOS / Linux：

```bash
bash scripts/run_harness.sh
```

Harness 報告會寫到：

```text
~/ai-outputs/04-技能驗證/x-article-reader
```

## 目前已實測的樣本

- `https://x.com/geoffintech/status/2042002590758572377`
- `https://x.com/rohit4verse/article/2041548810804211936`

## 限制

- `x.com/i/article/<id>` 這種直連不一定可讀，常常只會看到不支援頁面。
- 如果 user 只貼 `i/article` 連結，最穩的做法還是改拿原始 status URL。
- 目前 focus 是公開內容，不處理受保護帳號或需要登入權限的內容。

