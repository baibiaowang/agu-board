# A股公告看板 - 部署说明

## 快速开始（3分钟完成）

### 1. 创建仓库（30秒）

打开 https://github.com/new
- Repository name: `agu-board`
- Visibility: `Public`
- 勾选 `Add a README file`
- 点击 `Create repository`

### 2. 上传文件（1分钟）

在仓库页面：
1. 点击 `Add file` → `Upload files`
2. 选择本文件夹中**所有文件**（不要选 `.git` 文件夹）
3. 点击 `Commit changes`

### 3. 启用功能（1分钟）

**启用 Pages：**
- Settings → Pages → Deploy from a branch → `gh-pages` / `root` → Save

**启用 Actions：**
- Actions 标签 → 点击 `I understand...` 启用
- 选择 `Update A-Share Announcement Board` → `Run workflow`

### 4. 完成

等待 5-10 分钟后访问：
```
https://你的用户名.github.io/agu-board/dashboard.html
```

---

## 详细步骤

如果快速开始遇到问题，请参考 `DEPLOY.md` 获取详细说明。

## 文件说明

| 文件 | 说明 |
|------|------|
| `.github/workflows/` | 自动运行配置 |
| `scripts/` | 数据抓取脚本 |
| `reports/dashboard/` | 看板页面模板 |
| `requirements.txt` | Python 依赖 |
| `DEPLOY.md` | 详细部署文档 |
