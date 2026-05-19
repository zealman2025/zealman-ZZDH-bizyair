# 2分钱1图 BizyAir API 插件

## 注册与 API Key

1. 打开 **https://bizyair.cn/** ，注册或登录账号。
2. 在 BizyAir 个人中心按指引创建 / 复制 **API Key**。
3. 在本插件设置页「API Key」中粘贴保存，即可在字字动画内发起文生图 / 图生图。

## 2分钱计算（BizyAir 专业版示例，以官网为准）

- **¥89 专业版**：每月可获得 **450000** 金币（≈ **1 金币 = 0.0002 元 ≈ 0.02 分**）。
- **折合**：在上述档位下，约合 **每张图 2～5 分钱**（具体扣费与活动以 BizyAir 为准）。
- **可核对**：450000 ÷ 200 = **2250** 张/月（以 200 金币/张计的可生成张数）。

### 价目表

| 模式 | 分辨率 | 金币/张 | 折合 |
|---|---|---|---|
| **GPT-Image-2 文生图 / 图生图** | 1K / 2K / 4K 同价 | **100** | ≈ **2 分钱** |
| **NanoBanana 2（第三方）** | 1K | **200** | ≈ **4 分钱** |
| **NanoBanana 2（第三方）** | 2K | **200** | ≈ **4 分钱** |
| **NanoBanana 2（第三方）** | 4K | **250** | ≈ **5 分钱** |

> 价格随 BizyAir 官方调整而变化，本插件 UI 中显示的金币数与折合人民币仅作参考。

## 账户查询（插件内一键调用）

插件 UI 在「API Key」下方提供两个按钮：

- **查询用户信息** → `GET https://api.bizyair.cn/x/v1/user/metadata`
- **查询余额**     → `GET https://api.bizyair.cn/y/v1/wallet`

均使用 `Authorization: Bearer {BIZYAIR_API_KEY}` 鉴权，余额会同时按
`¥89 / 450000 金币` 单价折算成人民币显示。也可用 `curl` 自行验证：

```bash
curl -X GET "https://api.bizyair.cn/x/v1/user/metadata" \
  -H "Authorization: Bearer {BIZYAIR_API_KEY}"

curl -X GET "https://api.bizyair.cn/y/v1/wallet" \
  -H "Authorization: Bearer {BIZYAIR_API_KEY}"
```

## 插件存放目录

本插件需放在字字动画安装目录下的 **图像插件** 文件夹中，与本目录结构一致即可：

```
{字字动画安装目录}\_internal\plugins\image_plugins\bizyair_2fen_image\
    ├── info.json
    ├── main.py
    ├── README.md          （本说明）
    └── ui\
        └── index.html
```

将整个 `bizyair_2fen_image` 文件夹复制到上述路径后，重启软件（如需要）再在插件列表中选择使用。
