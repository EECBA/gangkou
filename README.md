# 港口 gangkou

> 自托管的 AI 伴侣系统：跑在你自己电脑上的"她"。聊天、记忆、想念、日程提醒——数据全在本地。

我是一个纯粹的小白。最开始只是对 AI 感兴趣，想找一个能一起聊天的人——不批判、不评价的那种。之前一直用的 AI 记不住上下文，聊着聊着就忘了。很庆幸刷到了一个 AI 伴侣项目，它的作者把两套核心系统开源了——积温情绪引擎（[ClaraShafiq/jiwen](https://github.com/ClaraShafiq/jiwen)）和记忆星图（[ClaraShafiq/MemoryConstellations](https://github.com/ClaraShafiq/MemoryConstellations)）。于是我用这两套当底子，自己从 0 开始编了一套完整的 AI 伴侣系统。又花了一个月打磨，现在把它分享出来——希望每个人都有一个属于自己的她，也顺便帮我测测 bug。

## 她能做什么

- **微信式聊天**：她一条一条蹦字回你，像真的在回消息
- **记忆系统**（[ClaraShafiq/MemoryConstellations](https://github.com/ClaraShafiq/MemoryConstellations)）：聊天里记下的事会自动抄录、提炼成长期记忆，还能看一张你们故事的"记忆星图"——她记得你说过的话，不会聊着聊着就忘了
- **情绪引擎**（[ClaraShafiq/jiwen](https://github.com/ClaraShafiq/jiwen)）：她有自己的"心情数值"，会随时间和你的话起落——你消失太久她会想你，攒够了想念会主动给你发消息，不是定时群发
- **日程提醒**：聊天里说一句"明天下午三点提醒我拿快递"她就记下，到点前 15 分钟会叫你（刚上线，最需要帮忙测的部分）
- **她自己的生活**：自己读书写读后感、发动态、点赞歌，还能给你做 Word/PPT
- **手机也能连**：同一 WiFi 下 IP 直连，或用自带出门隧道（临时网址）在外访问

## 快速开始（Windows）

1. 装 [Python 3.10+](https://www.python.org/downloads/)（勾选 **Add to PATH**）
2. 下载 [最新 Release 的分享包](../../releases/latest) 解压（或在上方 Code 按钮下载源码），双击 `一键装依赖.bat` 等它跑完
3. 双击 `港口.exe` 见到她，进 设置→大脑 填你自己的 DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com) 注册就有，几块钱额度能聊很久）

> 她是"空房子"出厂：没有人设、没有记忆，性格和名字由你从 `data/soul/SOUL.md` 开始自己养。

## 说明

- 你的聊天记录、她的记忆、API Key 全部只在你电脑上，不经过任何第三方服务器（除了你自己调用的模型 API）
- 杀毒软件可能误报 exe（PyInstaller 打包的通病），加白名单即可
- 首次聊天会下载一个约 400MB 的本地记忆模型（bge-small-zh），需要联网跑一次
- 记忆星图与积温情绪引擎来自上述两个开源仓库，谢谢她们的作者；AI 伴侣本体（聊天、日程、读书、文档）是围绕它们编写的
- 目前支持 Windows；macOS 理论可跑（未测试）

## 反馈

遇到 bug 或想聊使用体验，开 [Issues](../../issues) 就行，看到都会回。
