# LEO · 猜英语 — 部署到 Render（免费层）指南
# 让朋友先感受完整功能：三遍字幕流 / 级别选择 / 循环 / 阅读区
# 精选 12 集（982 场景，235MB）入门级内容，可放进 Render 免费层 512MB 磁盘

## 一、需要准备
1. **GitHub 账号**（免费）：https://github.com
2. **Render 账号**（免费，需绑一张信用卡验证，不扣费）：https://render.com

## 二、三步部署

### 第 1 步：把代码推到 GitHub
```bash
# 在你自己电脑上执行
cd /tmp/leo-render
git init
git add .
git commit -m "LEO 猜英语 · 精选体验版"
# 在 GitHub 建一个私有仓库 leo-guess-english，然后：
git remote add origin https://github.com/你的用户名/leo-guess-english.git
git push -u origin main
```

### 第 2 步：在 Render 创建服务
1. 打开 https://render.com → New → Web Service
2. 连接 GitHub → 选 leo-guess-english 仓库
3. Runtime 选 **Python 3**（Render 会自动用 render.yaml）
4. Plan 选 **Free**（512MB RAM / 512MB 磁盘）
5. 点 **Create Web Service**

### 第 3 步：等部署完成（约 3-5 分钟）
- Render 会给一个网址：`https://leo-guess-english.onrender.com`
- 把这个网址发给朋友，手机/电脑浏览器打开就能刷
- **注意**：免费层 15 分钟没人访问会休眠，下次打开等 ~30 秒冷启动，正常现象

## 三、常见问题

| 问题 | 解决 |
|---|---|
| 部署失败 | 看 Render 日志（Logs 标签），多半是磁盘/内存超限，可重试 |
| 中国访问慢 | 免费层只有海外节点，国内访问会慢；正式版建议用国内云服务器（deploy/README.md） |
| 想换内容 | 重新 `git push` 新代码，Render 自动重新部署 |
| 免费层流量 | 100GB/月免费，视频约 235MB，够几十个朋友刷好几轮 |

## 四、注意
- 免费层磁盘会在重新部署时**清空**（学习进度丢失）— 体验版可接受
- 本体验版是**精选内容**，完整入门级 98 集（约 2GB）建议上国内云服务器
