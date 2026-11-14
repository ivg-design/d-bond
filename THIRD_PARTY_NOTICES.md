# Third-party notices

## Codex Desktop IPC protocol reference

`adapters/codex-session/ipc.py` identifies the protocol shape documented by
[yieldspace/agmsg-codex-relay](https://github.com/yieldspace/agmsg-codex-relay),
including its `codex_app_ipc.py`, as a reference for D-Bond's independent
implementation. The original attribution in that file is retained. The upstream
MIT notice is reproduced below:

```text
MIT License

Copyright (c) 2026 sizumita

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
```

## Dot connector and Python runtime

The embedded MCP host includes unmodified portions of the MIT-licensed dots-bridge project. Its license, pinned revision and source inventory are included with the host. [Upstream notice](adapters/dot-host/UPSTREAM_NOTICE.md)

The Apple silicon download includes CPython and third-party Python libraries. Interpreter and native-library notices are included under the packaged runtime. Python distributions carry their own license metadata. [Runtime notices](scripts/dot-runtime/README.md)

## Service artwork

Service icons are resolved from installed apps or selected by the owner for a connection. [Connection artwork](Assets/ServiceIcons/README.md)
