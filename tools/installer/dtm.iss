; DTM Windows 安裝程式（Inno Setup 6）
; 先執行 python tools\build_windows.py 產生 dist\DTM-<版本>-win64\，再用 Inno Setup 編譯這個檔案：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tools\installer\dtm.iss
; 版本不同時改下面的 AppVer。
; 安裝到 C:\DTM（程式會在安裝資料夾寫入資料庫、設定檔與記錄檔，所以不放 Program Files）。

#define AppVer "2.1.0"
#define SrcDir "..\..\dist\DTM-" + AppVer + "-win64"

[Setup]
AppId={{5E3C2B0F-6A43-4C2E-9F0B-2D9C1D7A2101}
AppName=DTM 減速機健康監控
AppVersion={#AppVer}
AppVerName=DTM {#AppVer}（Beta）
AppPublisher=Domingo He
DefaultDirName={sd}\DTM
DisableProgramGroupPage=yes
DefaultGroupName=DTM
OutputDir=..\..\dist
OutputBaseFilename=DTM-{#AppVer}-setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
LicenseFile={#SrcDir}\LICENSE
UninstallDisplayName=DTM {#AppVer}
; 移除時保留使用者資料（資料庫、設定檔、備份、記錄檔）

[Languages]
; 安裝畫面語言：預設英文（Inno Setup 一定有）。要繁體中文介面：從 Inno Setup 網站「Translations」下載
; ChineseTraditional.isl 放到 Inno Setup 的 Languages 資料夾，再把下一行換成註解裡那一行。
Name: "english"; MessagesFile: "compiler:Default.isl"
; Name: "chinesetraditional"; MessagesFile: "compiler:Languages\ChineseTraditional.isl"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\DTM 模擬器試跑"; Filename: "{app}\start_sim.bat"; WorkingDir: "{app}"
Name: "{group}\DTM 中央畫面演示"; Filename: "{app}\start_central_demo.bat"; WorkingDir: "{app}"
Name: "{group}\DTM 小型模式（10 台以下）"; Filename: "{app}\start_small.bat"; WorkingDir: "{app}"
Name: "{group}\DTM 單機監控"; Filename: "{app}\start.bat"; WorkingDir: "{app}"
Name: "{group}\說明（README）"; Filename: "{app}\README.zh-TW.md"
Name: "{group}\README (English)"; Filename: "{app}\README.md"
Name: "{group}\移除 DTM"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\start_sim.bat"; Description: "用模擬器試跑一次"; Flags: postinstall shellexec skipifsilent unchecked
