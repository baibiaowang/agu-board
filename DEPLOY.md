# A股公告看板 - 部署工具

## 快速部署（推荐）

### 方法：使用 GitHub 网页界面（无需命令行）

#### 步骤 1: 创建仓库

1. 打开浏览器访问：https://github.com/new
2. 填写信息：
   - **Repository name**: `agu-board`
   - **Description**: `A股公告看板`
   - **Visibility**: `Public`（公共仓库免费）
   - **勾选**: `Add a README file`
3. 点击 **Create repository**

#### 步骤 2: 上传代码

**方法 A - 拖拽上传（最简单）**

1. 在仓库页面点击 **Add file** → **Upload files**
2. 打开文件浏览器，进入 `agu-board-temp` 文件夹
3. **选择所有文件**（Ctrl+A），**排除**以下文件：
   - `.git` 文件夹（如果有）
   - `deploy-to-github.bat`
   - `deploy-to-github.ps1`
   - `手动部署指南.bat`
4. 拖拽到 GitHub 页面，或点击 **choose your files** 选择
5. 等待上传完成，点击 **Commit changes**

**方法 B - 命令行（适合熟悉 Git 的用户）**

```bash
# 1. 克隆仓库
git clone https://github.com/你的用户名/agu-board.git
cd agu-board

# 2. 复制项目文件（将 agu-board-temp 中的文件复制到此目录）
# Windows: xcopy /E "..\agu-board-temp\*" .
# Mac/Linux: cp -r ../agu-board-temp/* .

# 3. 提交代码
git add .
git commit -m "Initial commit"
git push origin main
```

#### 步骤 3: 启用 GitHub Pages

1. 进入仓库，点击 **Settings**（设置）
2. 左侧菜单点击 **Pages**
3. 配置：
   - **Source**: `Deploy from a branch`
   - **Branch**: `gh-pages` / `(root)`
4. 点击 **Save**

#### 步骤 4: 启用并触发 Actions

1. 点击仓库的 **Actions** 标签
2. 如果看到提示，点击 **I understand my workflows, go ahead and enable them**
3. 在左侧选择 **Update A-Share Announcement Board**
4. 点击右侧 **Run workflow** → **Run workflow**
5. 等待运行完成（约 5-10 分钟）

#### 步骤 5: 访问看板

运行成功后，访问：
```
https://你的用户名.github.io/agu-board/dashboard.html
```

## 验证部署

### 检查清单

- [ ] 仓库已创建：https://github.com/你的用户名/agu-board
- [ ] 代码已上传：仓库中有 `.github/workflows/update-board.yml`
- [ ] Pages 已启用：Settings → Pages 显示绿色提示
- [ ] Actions 已运行：Actions 标签显示绿色 ✓
- [ ] 看板可访问：https://你的用户名.github.io/agu-board/dashboard.html

### 常见问题

**Q: 看板页面显示 404**
- 确认 Actions 已成功运行（绿色 ✓）
- 确认 GitHub Pages 已启用
- 等待 5-10 分钟让 CDN 生效
- 强制刷新页面（Ctrl+F5）

**Q: 数据未更新**
- 检查 Actions 是否正常运行
- 手动触发运行：Actions → Run workflow
- 检查是否触发巨潮限流（403 错误）

**Q: Actions 运行失败**
- 点击失败的运行记录查看日志
- 常见原因：巨潮限流（正常现象，会自动重试）

## 后续维护

### 自动更新
- 已配置每天自动运行 3 次（9:00/15:30/21:00）
- 无需手动操作

### 手动触发
- 进入 Actions 页面
- 选择 Update A-Share Announcement Board
- 点击 Run workflow

### 修改配置
- 编辑 `.github/workflows/update-board.yml` 修改定时频率
- 编辑 `scripts/cninfo_fetch.py` 修改关键词筛选

## 获取帮助

- GitHub Actions 文档：https://docs.github.com/cn/actions
- GitHub Pages 文档：https://docs.github.com/cn/pages
- 提交 Issue：https://github.com/你的用户名/agu-board/issues
