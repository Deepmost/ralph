---
name: agent-browser
description: Automates browser interactions via CDP (Chrome DevTools Protocol). Use when the user needs to navigate websites, interact with web pages, fill forms, take screenshots, test web applications, or extract information from web pages.
allowed-tools: Bash(agent-browser:*),Bash(curl:*),Bash(export:*)
---

# Browser Automation with agent-browser (CDP Mode)

## Prerequisites

需要先确保 Chrome 浏览器已开启远程调试端口 9222。

### 启动方式（macOS / Linux）

在项目根目录执行 Ralph 自带的脚本，它会启动（或复用）一个已开启 CDP 的 Chrome 实例：

```bash
bash scripts/ralph/chrome-cdp.sh 9222
```

脚本会输出 `WS_URL`，可直接用于后续的 `--cdp` 参数；若端口 9222 已有可用实例则自动复用，不会重复启动。

## 禁止事项（常见错误）

- **禁止** 使用 `agent-browser connect <port>` 或 `agent-browser connect <ws_url>` 命令
- **禁止** 通过 Node.js `require('agent-browser')` 调用，agent-browser 是原生二进制 CLI
- **禁止** 不带 `--cdp "$WS_URL"` 直接执行 `agent-browser open <url>`
- **唯一正确方式**：每条命令都用 `agent-browser --cdp "$WS_URL" <command>` 格式

## 连接规则（重要）

本 skill 固定使用 CDP 端口 **9222**。`--cdp` 参数**不支持纯端口号**（如 `--cdp 9222`），**必须传完整的 WebSocket URL**（如 `--cdp "ws://localhost:9222/devtools/browser/xxx"`）。

每次会话开始时，先动态获取 `WS_URL`，后续所有命令都用 `--cdp "$WS_URL"`：

```bash
WS_URL=$(curl -s http://localhost:9222/json/version | tr -d '\r\n ' | sed 's/.*webSocketDebuggerUrl":"//;s/".*//')
```

为防止多个 Chrome 实例并存时窜台，禁止省略 `--cdp` 参数，也不要依赖 session 缓存的默认连接。

## Quick Start（必须严格按此流程执行）

```bash
# 1. 获取 WebSocket URL（每次会话执行一次即可）
WS_URL=$(curl -s http://localhost:9222/json/version | tr -d '\r\n ' | sed 's/.*webSocketDebuggerUrl":"//;s/".*//')

# 2. 连接并打开页面
agent-browser --cdp "$WS_URL" open <url>

# 3. 每条后续命令都必须带 --cdp "$WS_URL"
agent-browser --cdp "$WS_URL" snapshot -i       # Get interactive elements with refs
agent-browser --cdp "$WS_URL" click @e1         # Click element by ref
agent-browser --cdp "$WS_URL" fill @e2 "text"   # Fill input by ref
```

## Core workflow (CDP Mode)

1. 获取 WS URL: `WS_URL=$(curl -s http://localhost:9222/json/version | tr -d '\r\n ' | sed 's/.*webSocketDebuggerUrl":"//;s/".*//')`
2. 连接并导航: `agent-browser --cdp "$WS_URL" open <url>`
3. 获取快照: `agent-browser --cdp "$WS_URL" snapshot -i` (返回带 ref 的元素如 `@e1`, `@e2`)
4. 使用 ref 进行交互（每条命令都带 `--cdp "$WS_URL"`）
5. 页面变化后重新获取快照

## Commands

### CDP Connection
```bash
# 验证 CDP 端口可用
curl -s http://localhost:9222/json/version

# 通过完整的 WebSocket URL 连接（推荐，每条命令都带）
agent-browser --cdp "$WS_URL" open <url>
agent-browser --cdp "$WS_URL" snapshot -i
```

### Navigation
```bash
agent-browser --cdp "$WS_URL" open <url>      # Navigate to URL
agent-browser --cdp "$WS_URL" back            # Go back
agent-browser --cdp "$WS_URL" forward         # Go forward
agent-browser --cdp "$WS_URL" reload          # Reload page
```

### Snapshot (page analysis)
```bash
agent-browser --cdp "$WS_URL" snapshot            # Full accessibility tree
agent-browser --cdp "$WS_URL" snapshot -i         # Interactive elements only (recommended)
agent-browser --cdp "$WS_URL" snapshot -c         # Compact output
agent-browser --cdp "$WS_URL" snapshot -d 3       # Limit depth to 3
agent-browser --cdp "$WS_URL" snapshot -s "#main" # Scope to CSS selector
```

### Interactions (use @refs from snapshot)
```bash
agent-browser --cdp "$WS_URL" click @e1           # Click
agent-browser --cdp "$WS_URL" dblclick @e1        # Double-click
agent-browser --cdp "$WS_URL" focus @e1           # Focus element
agent-browser --cdp "$WS_URL" fill @e2 "text"     # Clear and type
agent-browser --cdp "$WS_URL" type @e2 "text"     # Type without clearing
agent-browser --cdp "$WS_URL" press Enter         # Press key
agent-browser --cdp "$WS_URL" press Control+a     # Key combination
agent-browser --cdp "$WS_URL" keydown Shift       # Hold key down
agent-browser --cdp "$WS_URL" keyup Shift         # Release key
agent-browser --cdp "$WS_URL" hover @e1           # Hover
agent-browser --cdp "$WS_URL" check @e1           # Check checkbox
agent-browser --cdp "$WS_URL" uncheck @e1         # Uncheck checkbox
agent-browser --cdp "$WS_URL" select @e1 "value"  # Select dropdown
agent-browser --cdp "$WS_URL" scroll down 500     # Scroll page
agent-browser --cdp "$WS_URL" scrollintoview @e1  # Scroll element into view
agent-browser --cdp "$WS_URL" drag @e1 @e2        # Drag and drop
agent-browser --cdp "$WS_URL" upload @e1 file.pdf # Upload files
```

### Get information
```bash
agent-browser --cdp "$WS_URL" get text @e1        # Get element text
agent-browser --cdp "$WS_URL" get html @e1        # Get innerHTML
agent-browser --cdp "$WS_URL" get value @e1       # Get input value
agent-browser --cdp "$WS_URL" get attr @e1 href   # Get attribute
agent-browser --cdp "$WS_URL" get title           # Get page title
agent-browser --cdp "$WS_URL" get url             # Get current URL
agent-browser --cdp "$WS_URL" get count ".item"   # Count matching elements
agent-browser --cdp "$WS_URL" get box @e1         # Get bounding box
```

### Check state
```bash
agent-browser --cdp "$WS_URL" is visible @e1      # Check if visible
agent-browser --cdp "$WS_URL" is enabled @e1      # Check if enabled
agent-browser --cdp "$WS_URL" is checked @e1      # Check if checked
```

### Screenshots & PDF
```bash
agent-browser --cdp "$WS_URL" screenshot          # Screenshot to stdout
agent-browser --cdp "$WS_URL" screenshot path.png # Save to file
agent-browser --cdp "$WS_URL" screenshot --full   # Full page
agent-browser --cdp "$WS_URL" pdf output.pdf      # Save as PDF
```

### Video recording
```bash
agent-browser --cdp "$WS_URL" record start ./demo.webm    # Start recording
agent-browser --cdp "$WS_URL" click @e1                   # Perform actions
agent-browser --cdp "$WS_URL" record stop                 # Stop and save video
agent-browser --cdp "$WS_URL" record restart ./take2.webm # Stop current + start new
```
Recording creates a fresh context but preserves cookies/storage from your session. If no URL is provided, it automatically returns to your current page. For smooth demos, explore first, then start recording.

### Wait
```bash
agent-browser --cdp "$WS_URL" wait @e1                     # Wait for element
agent-browser --cdp "$WS_URL" wait 2000                    # Wait milliseconds
agent-browser --cdp "$WS_URL" wait --text "Success"        # Wait for text
agent-browser --cdp "$WS_URL" wait --url "**/dashboard"    # Wait for URL pattern
agent-browser --cdp "$WS_URL" wait --load networkidle      # Wait for network idle
agent-browser --cdp "$WS_URL" wait --fn "window.ready"     # Wait for JS condition
```

### Mouse control
```bash
agent-browser --cdp "$WS_URL" mouse move 100 200      # Move mouse
agent-browser --cdp "$WS_URL" mouse down left         # Press button
agent-browser --cdp "$WS_URL" mouse up left           # Release button
agent-browser --cdp "$WS_URL" mouse wheel 100         # Scroll wheel
```

### Semantic locators (alternative to refs)
```bash
agent-browser --cdp "$WS_URL" find role button click --name "Submit"
agent-browser --cdp "$WS_URL" find text "Sign In" click
agent-browser --cdp "$WS_URL" find label "Email" fill "user@test.com"
agent-browser --cdp "$WS_URL" find first ".item" click
agent-browser --cdp "$WS_URL" find nth 2 "a" text
```

### Browser settings
```bash
agent-browser --cdp "$WS_URL" set viewport 1920 1080      # Set viewport size
agent-browser --cdp "$WS_URL" set device "iPhone 14"      # Emulate device
agent-browser --cdp "$WS_URL" set geo 37.7749 -122.4194   # Set geolocation
agent-browser --cdp "$WS_URL" set offline on              # Toggle offline mode
agent-browser --cdp "$WS_URL" set headers '{"X-Key":"v"}' # Extra HTTP headers
agent-browser --cdp "$WS_URL" set credentials user pass   # HTTP basic auth
agent-browser --cdp "$WS_URL" set media dark              # Emulate color scheme
```

### Cookies & Storage
```bash
agent-browser --cdp "$WS_URL" cookies                     # Get all cookies
agent-browser --cdp "$WS_URL" cookies set name value      # Set cookie
agent-browser --cdp "$WS_URL" cookies clear               # Clear cookies
agent-browser --cdp "$WS_URL" storage local               # Get all localStorage
agent-browser --cdp "$WS_URL" storage local key           # Get specific key
agent-browser --cdp "$WS_URL" storage local set k v       # Set value
agent-browser --cdp "$WS_URL" storage local clear         # Clear all
```

### Network
```bash
agent-browser --cdp "$WS_URL" network route <url>              # Intercept requests
agent-browser --cdp "$WS_URL" network route <url> --abort      # Block requests
agent-browser --cdp "$WS_URL" network route <url> --body '{}'  # Mock response
agent-browser --cdp "$WS_URL" network unroute [url]            # Remove routes
agent-browser --cdp "$WS_URL" network requests                 # View tracked requests
agent-browser --cdp "$WS_URL" network requests --filter api    # Filter requests
```

### Tabs & Windows
```bash
agent-browser --cdp "$WS_URL" tab                 # List tabs
agent-browser --cdp "$WS_URL" tab new [url]       # New tab
agent-browser --cdp "$WS_URL" tab 2               # Switch to tab
agent-browser --cdp "$WS_URL" tab close           # Close tab
agent-browser --cdp "$WS_URL" window new          # New window
```

### Frames
```bash
agent-browser --cdp "$WS_URL" frame "#iframe"     # Switch to iframe
agent-browser --cdp "$WS_URL" frame main          # Back to main frame
```

### Dialogs
```bash
agent-browser --cdp "$WS_URL" dialog accept [text]  # Accept dialog
agent-browser --cdp "$WS_URL" dialog dismiss        # Dismiss dialog
```

### JavaScript
```bash
agent-browser --cdp "$WS_URL" eval "document.title"   # Run JavaScript
```

## Example: Form submission (CDP Mode)

```bash
# 1. 验证 9222 端口可用
curl -s http://localhost:9222/json/version

# 2. 打开表单页面
agent-browser --cdp "$WS_URL" open https://example.com/form
agent-browser --cdp "$WS_URL" snapshot -i
# Output shows: textbox "Email" [ref=e1], textbox "Password" [ref=e2], button "Submit" [ref=e3]

# 3. 填写并提交表单
agent-browser --cdp "$WS_URL" fill @e1 "user@example.com"
agent-browser --cdp "$WS_URL" fill @e2 "password123"
agent-browser --cdp "$WS_URL" click @e3
agent-browser --cdp "$WS_URL" wait --load networkidle
agent-browser --cdp "$WS_URL" snapshot -i  # Check result
```

## Example: Authentication with saved state

```bash
# Login once
agent-browser --cdp "$WS_URL" open https://app.example.com/login
agent-browser --cdp "$WS_URL" snapshot -i
agent-browser --cdp "$WS_URL" fill @e1 "username"
agent-browser --cdp "$WS_URL" fill @e2 "password"
agent-browser --cdp "$WS_URL" click @e3
agent-browser --cdp "$WS_URL" wait --url "**/dashboard"
agent-browser --cdp "$WS_URL" state save auth.json

# Later sessions: load saved state
agent-browser --cdp "$WS_URL" state load auth.json
agent-browser --cdp "$WS_URL" open https://app.example.com/dashboard
```

## Sessions (parallel browsers)

不要使用 `--session` 来区分多端口实例。始终通过动态获取的 `$WS_URL` 显式指定连接即可。

## JSON output (for parsing)

Add `--json` for machine-readable output:
```bash
agent-browser --cdp "$WS_URL" snapshot -i --json
agent-browser --cdp "$WS_URL" get text @e1 --json
```

## Debugging

```bash
# CDP 连接调试
curl -s http://localhost:9222/json/version    # 检查 CDP 端口是否可用
agent-browser --cdp "$WS_URL" snapshot             # 通过端口连接

# 页面调试
agent-browser --cdp "$WS_URL" console                         # View console messages
agent-browser --cdp "$WS_URL" console --clear                 # Clear console
agent-browser --cdp "$WS_URL" errors                          # View page errors
agent-browser --cdp "$WS_URL" errors --clear                  # Clear errors
agent-browser --cdp "$WS_URL" highlight @e1                   # Highlight element
agent-browser --cdp "$WS_URL" trace start                     # Start recording trace
agent-browser --cdp "$WS_URL" trace stop trace.zip            # Stop and save trace
```
