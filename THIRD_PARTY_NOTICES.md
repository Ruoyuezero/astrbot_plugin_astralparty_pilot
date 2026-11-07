# 第三方代码与许可声明

本项目在「联机查号」相关的协议层，参考并改写了下列开源项目的代码与结论。
按 MIT 许可要求，随分发保留其版权声明与许可全文。

---

## AstralParty_Dex（星趴档案）

- 仓库：https://github.com/ZtyanCrany/AstralParty_Dex
- 作者：Nemophila & ZtyanCrany
- 许可：MIT
- 本项目用到的部分：
  - `pkg/liveapi.py`：收包循环 `_recv_loop`、等待响应 `_wait_for`、varint 解码
    `_read_varint`，以及包头结构 / CMDID / 登录字段等协议结论
  - `pkg/feimo.py`：签名拼参 `make_sign`、嵌套响应取值 `_pick`
  - `pkg/feimo_signkeys.py`：渠道签名密钥表（常量取自游戏官方客户端）
- 改动方式：按本项目的异常类型、日志与接口做了改写，逻辑结构与之同源。

---

## MIT License

Copyright (c) 2026 Nemophila & ZtyanCrany

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
