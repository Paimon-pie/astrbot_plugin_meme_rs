# astrbot_plugin_meme_rs

用于接入本地或远程 [meme-generator-rs](https://github.com/MemeCrafters/meme-generator-rs) HTTP 服务的 AstrBot 表情包制作插件，已适配 QQ 官方机器人。

> 说明：本插件为个人自用、随缘维护，AI 辅助编写并维护，未经过充分全面测试，使用中如遇问题请自行调整或按需修改。

## 安装方式

1. 自行部署并启动 `meme-generator-rs` HTTP 服务（本地或云端均可，默认地址 `http://127.0.0.1:2255`）。
2. 在 AstrBot 插件管理中通过本仓库地址安装，或将整个 `astrbot_plugin_meme_rs` 文件夹放到：
   ```text
   <AstrBot目录>/data/plugins/astrbot_plugin_meme_rs
   ```
3. 重载插件后，在插件配置中填写你的 `base_url` 即可使用。

## 主要功能

- 自动从 `meme-generator-rs` 同步全部表情关键词、参数、默认文本与服务端自带 `shortcuts`
- 支持直接发图、引用图片、`@某人`、手写 `@QQ号` 及发送者默认头像
- 适配 QQ 官方机器人：自动识别 OpenID 头像与 `<@OPENID>` 艾特，支持表情同消息 `+1` 按钮及 Markdown 帮助按钮菜单
- 支持多段文本（`/` 分隔）与参数传递（`#参数名 值` / `--参数名 值`）
- 支持 WebUI 可搜索多选标签式禁用表情列表、强制前缀模式与断连自动重连

## 常用指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/meme列表` | `/表情列表` | 查看全部表情列表图 |
| `/meme搜索 <关键词>` | `/表情搜索` | 搜索表情 |
| `/meme详情 <关键词>` | `/表情详情` | 查看参数与预览图 |
| `/随机表情` | `/随机meme` | 随机生成一张表情包 |
| `/制作示例` | `/表情示例`、`/meme示例` | 查看表情制作示例 |
| `/meme刷新` | `/表情刷新` | 重新从服务端同步表情库 |
| `/meme状态` | `/表情状态` | 检查服务连通性与延迟 |
| `/meme帮助` | `/表情帮助` | 查看插件帮助（QQ 官 Bot 自动发 Markdown + 按钮） |

## 制作示例

- `hello`（或 `hello @某人` / 回复图片发 `hello`）
- `5000兆 我去/洛天依`（多段文本用 `/` 分隔）
- 带参数：`表情关键词 #参数名 值` 或 `--参数名 值`（如：`对称 #direction top`）

## 致谢

- [MemeCrafters/meme-generator-rs](https://github.com/MemeCrafters/meme-generator-rs)
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)

## 开源协议

[MIT License](LICENSE)
