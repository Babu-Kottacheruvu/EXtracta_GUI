# Builds the desktop client (main_gui.py) into a single-file Windows .exe.
# Run from PowerShell in this folder:
#   .\build_client.ps1
#
# Prerequisites:
#   pip install -r requirements-client.txt pyinstaller
#
# Output: dist\EXtracta.exe
#
# Note on WebView2: pywebview uses the Edge WebView2 runtime on Windows.
# It's preinstalled on current Windows 10/11, but if you're targeting older
# or locked-down machines, have users install the "Evergreen Bootstrapper"
# from Microsoft first: https://developer.microsoft.com/microsoft-edge/webview2/

pyinstaller --noconfirm --onefile --windowed `
    --name EXtracta `
    --collect-all webview `
    main_gui.py

Write-Host "Build complete: dist\EXtracta.exe"
Write-Host "Drop a server_url.txt next to it to point at a different backend without rebuilding."
