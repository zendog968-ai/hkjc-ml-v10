# 將 V10.1 專案推送至 GitHub 私人儲存庫

本專案已完成 `git init`，並已暫存所有允許版本控制的程式碼、說明與設定範本。`.gitignore` 會排除 SQLite／資料庫、模型、日誌、ZIP、CSV、HTML、日常預測輸入輸出及其他生成檔案。

> 推送前請先在 GitHub 建立一個**空白的私人儲存庫**；建立時不要初始化 README、`.gitignore` 或 License，以下命令最直接。

```bash
cd /home/ubuntu/hkjc_v10_database

# 只需首次設定本專案的 Git 提交身份；請填入自己的資料。
git config user.name "你的 GitHub 顯示名稱"
git config user.email "你的 GitHub 已驗證電郵"

# 建立首個本機提交。
git commit -m "Initial commit: V10.1 racing ML system"

# 將以下 URL 替換為你已建立的私人儲存庫 HTTPS URL。
git remote add origin https://github.com/你的帳號/你的私人儲存庫.git

git push -u origin main
```

如私人儲存庫已經有 README 或其他首次提交，完成 `git remote add origin ...` 及本機提交後，改用以下命令合併遠端歷史，再推送：

```bash
git pull --rebase origin main
git push -u origin main
```

首次透過 HTTPS 推送時，GitHub 可能要求登入或 Personal Access Token；請依終端機提示完成。不要把 Token、資料庫、模型、即時賠率覆蓋檔或預測 JSON 寫入此儲存庫。

## 推送前核對

```bash
git status --short
git diff --cached --name-only
git check-ignore -v hkjc_last_season.sqlite horse_model.pkl full_etl_v3.log race_card.json prediction.json
```

預期可追蹤檔案只有 `.py`、`.md`、`requirements.txt` 與兩份 JSON 設定範本；關鍵資料、模型與日常輸出應顯示為被 `.gitignore` 忽略。

## 參考資料

[1] [GitHub Docs: Adding a remote repository](https://docs.github.com/en/get-started/getting-started-with-git/managing-remote-repositories)

[2] [GitHub Docs: Pushing commits to a remote repository](https://docs.github.com/en/get-started/using-git/pushing-commits-to-a-remote-repository)
