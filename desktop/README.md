# 原生桌面

## Windows 便携版

从 [GitHub Releases](https://github.com/fenghansen/WebCompare/releases) 下载 Windows x64 ZIP。解压整个目录，双击 `WebCompare.exe`。窗口使用系统 WebView2 Runtime 和 x64 Visual C++ v14 运行库。后者可从[微软官方分发页](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)安装。需要应用菜单和快捷方式时，可以运行同版本的 setup.exe。

## Linux 普通使用

多数远程服务器没有桌面环境，首选根目录 [README 的 Web/SSH 使用方式](../README.md)，在本机浏览器看图。

已有图形会话并安装 GTK3 / WebKitGTK 4.1 的 Ubuntu 24.04 x86_64，可以直接解压 portable 包运行，无需 sudo，也无需安装 Python、Node.js 或编译器：

```sh
tar -xzf webcompare-desktop-0.1.1-linux-x64.tar.gz
cd webcompare-desktop-0.1.1
./webcompare-desktop
```

Ubuntu 桌面壳播放 H.264 MP4 需系统提供 `gstreamer1.0-libav`。用便携包时，可运行 `sudo apt install gstreamer1.0-libav` 补齐。用 `apt install` 安装 DEB 时，默认随推荐依赖安装。

运行 `./install-menu.sh` 可添加当前用户的应用菜单入口；之后保留此解压目录。默认用户数据位于 `~/.config/WebCompare Native`，可用 `--user-data-dir /path/to/state` 指定独立目录。

有 sudo 权限可用 DEB 安装应用和 GTK/WebKit 运行库：

```sh
sudo apt install ./webcompare-desktop_*.deb
```

## 桌面控制命令

Linux 安装 `.deb` 后，从任意目录运行 `webcompare-ctl --help`。便携版在包目录运行 `./webcompare-ctl --help`。

Windows 安装版可在 PowerShell 中通过默认安装路径调用。

```powershell
& "$env:LOCALAPPDATA\Programs\WebCompare\webcompare-ctl.cmd" --help
```

Windows 便携版在解压目录运行 `.\webcompare-ctl.cmd --help`。

## 开发者构建 Linux 包

桌面壳使用 pywebview；Linux 使用系统 GTK3 / WebKitGTK 4.1，Windows 使用系统 WebView2。窗口、后端和图像工作进程共用一个冻结的 Python 3.12 运行时。

以下命令用于制作发行包。构建机需要 Python 3.12、Node.js/npm、C 编译器、pkg-config、GTK 的 Python 绑定和 WebKitGTK 4.1 开发文件。在仓库根目录执行：

```sh
python3.12 -m venv --system-site-packages .runtime/native-build-venv
.runtime/native-build-venv/bin/python -m pip install -r desktop/requirements-native-build.txt
npm --prefix frontend ci
npm --prefix frontend run build
.runtime/native-build-venv/bin/python desktop/build-native.py
```

缺少这些开发组件且有 sudo 权限时，可以先安装：

```sh
sudo apt install python3.12-venv python3-gi python3-cairo python3-apt build-essential pkg-config libwebkit2gtk-4.1-dev
```

WebKit 开发依赖也可解包到独立 sysroot，再用 `--webkit-sysroot /path/to/sysroot` 指定；不影响运行系统的库。`--output /path/to/output` 指定产物目录，默认是 `dist/native`。

构建完成后，在 `dist/native` 取得 tar.gz、DEB 和校验清单。桌面依赖的对应源码与同版本安装包一同发布。

## 开发者构建 Windows 包

在 Windows x64 上准备 Python 3.12、Node.js、WebView2 Runtime 和桌面构建依赖，执行前端构建后运行 `python desktop/build-native.py --skip-installer` 生成便携 ZIP。安装 Inno Setup 后运行 `python desktop/build-native.py` 可以同时生成 setup.exe。
