[Setup]
AppName=클래스카드 매크로
AppVersion=2.0.1
DefaultDirName={autopf}\ClasscardMacro
DefaultGroupName=클래스카드 매크로
OutputDir=.
OutputBaseFilename=Classcard_Setup
Compression=lzma
SolidCompression=yes
; 설치/삭제 화면 문구를 전부 한국어로 (언어 선택 창은 안 띄움)
ShowLanguageDialog=no
UninstallDisplayName=클래스카드 매크로
DisableProgramGroupPage=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Files]
; dist\main.exe를 가져와서 설치 폴더에는 암기.exe로 복사
Source: "dist\main.exe"; DestDir: "{app}"; DestName: "암기.exe"; Flags: ignoreversion

[Icons]
; 바탕화면 바로가기
Name: "{autodesktop}\클래스카드 매크로"; Filename: "{app}\암기.exe"
; 시작 메뉴 바로가기
Name: "{group}\클래스카드 매크로"; Filename: "{app}\암기.exe"
Name: "{group}\클래스카드 매크로 삭제"; Filename: "{uninstallexe}"

[Run]
; 설치 완료 후 매크로 실행
Filename: "{app}\암기.exe"; Description: "클래스카드 매크로 실행하기"; Flags: postinstall nowait skipifsilent