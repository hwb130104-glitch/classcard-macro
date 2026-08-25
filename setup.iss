[Setup]
AppName=클래스카드 오토 매크로
AppVersion=1.3
DefaultDirName={autopf}\ClasscardMacro
DefaultGroupName=클래스카드 매크로
OutputDir=.
OutputBaseFilename=Classcard_Setup
Compression=lzma
SolidCompression=yes

[Files]
; 매크로 메인 프로그램 (암기.exe)
Source: "암기.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 바탕화면에 바로가기 생성
Name: "{autodesktop}\클래스카드 매크로"; Filename: "{app}\암기.exe"

[Run]
; 설치 완료 후 매크로 실행
Filename: "{app}\암기.exe"; Description: "클래스카드 매크로 실행하기"; Flags: postinstall nowait skipifsilent
