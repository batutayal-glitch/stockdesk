# 個股觀察室（GitHub Pages 版）

這個資料夾就是完整網站，另外加上每天自動更新資料的排程。

## 上線步驟
1. 在 GitHub 建一個新的 repo，把這個資料夾裡的所有檔案上傳上去，包含隱藏的 `.github` 資料夾和 `.nojekyll`。
2. 到 Settings → Pages，Source 選「Deploy from a branch」，Branch 選 `main`，資料夾選 `/ (root)`，按 Save。
3. 到 Settings → Actions → General，Workflow permissions 選「Read and write permissions」。
4. 到 Actions 分頁，選「每日更新個股資料」，按 Run workflow。第一次會回補約 5 個月的資料，大約要 15～25 分鐘。
5. 完成後，網址是 `https://<你的帳號>.github.io/<repo 名稱>/`。手機要用這個新網址重新「加入主畫面」。

之後每個交易日晚上 10 點多會自動更新。打開網站就是最新資料，不用再手動匯入。

## 跟原版不同的地方
- `update_data.py`：每天抓證交所、櫃買中心、集保、公開資訊觀測站的資料，重新產生 `week-data.js`、`week-data.json`、`week-audit.json`。
- `sw.js`：資料檔每次都繞過瀏覽器快取，抓最新版；快取版本升到 v6。
- `data/history.json`：第一次執行後才會出現。集保的週資料要靠它累積，請不要刪掉。

## 不用 GitHub 的話
在自己電腦執行 `python3 update_data.py`，再到網站用匯入功能載入產生的 `stock-data.csv`。

## 已知限制
- 上櫃 3 檔（6147、1815、3441）的成交量來源是「張」，換算成股數只是約數，所以「價量趨勢」和「法人力道」會顯示「待精確值」。
- 「持股結構」要累積 3 週集保資料才會亮燈。
- 「重大事件」需要人工查核。
- 如果 Actions 執行紀錄顯示證交所連線一直失敗（GitHub 的主機在海外），就改用上面「不用 GitHub」的方式。
