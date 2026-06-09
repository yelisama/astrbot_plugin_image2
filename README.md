# astrbot_plugin_image2

QQ 平台 AstrBot 图片生成插件，保留文生图、图生图/改图、自拍参考图和单图 LLM tools。

## 功能

- `/aiimg`、`/生图`：文生图
- `/aiedit`、`/改图`、`/图生图`、`/修图`：使用当前消息、回复消息、@ 用户头像中的图片改图
- `/自拍`：使用自拍参考图生成新自拍
- `/自拍参考`：设置参考图；`/自拍参考 查看`；`/自拍参考 删除`
- `/重发图片`：重发最近一次生成成功的图片
- 保留 OpenAI Images 兼容 provider chain，可配置多个后端顺序兜底
- 保留 `gitee_draw_image`、`gitee_edit_image`、`aiimg_generate` 三个单图 LLM tools 以兼容旧调用

## 不包含

- 视频生成
- 批量生成命令
- 批量 LLM tool

## 配置重点

默认 provider 使用 `template_key: image2`，实际走 OpenAI Images 兼容接口：

```json
{
  "id": "image2",
  "template_key": "image2",
  "base_url": "https://your-image2-endpoint/v1",
  "api_key": "YOUR_KEY",
  "model": "image2",
  "supports_edit": true
}
```

`features.draw.chain`、`features.edit.chain` 和 `features.selfie.chain` 可以按 provider id 配置兜底顺序。权限拒绝会静默失败；并发满时会发送：`当前你的生图任务较多，请稍后再试。`
