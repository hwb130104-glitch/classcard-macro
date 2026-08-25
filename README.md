# 클래스카드 매크로

클래스카드(classcard.net) 단어장의 **암기 학습**과 **리콜 학습**을 자동화하는 Windows용 데스크톱 프로그램입니다.
Python(Tkinter) GUI + Selenium(브라우저 자동화) + PyAutoGUI(키/마우스 입력)로 동작합니다.

> **그냥 쓰기만 하실 분**은 아래 요구사항/설치 안내를 볼 필요 없이, [Releases](../../releases)에서 설치 파일(`Classcard_Setup.exe`)만 받아서 설치하면 바로 사용할 수 있습니다. Python이나 pip 설치가 필요 없습니다.
> 아래 안내는 소스코드를 직접 실행하거나 수정하고 싶은 분들을 위한 것입니다.

## 기능

-  암기 학습 모드: 암기 학습창 좌표를 등록해두면, `space` → `shift+space` → `→` 키를 반복 입력해 카드를 자동으로 넘깁니다.
-  리콜 학습 모드: Chrome을 띄워 리콜(4지선다) 문제 화면을 인식하고, 정답을 자동으로 선택합니다.
- 두 모드 모두 단어장 페이지에서 아래 북마크릿으로 추출한 단어 데이터를 사용합니다.

## 요구사항

- Python 3.x (일반사용자는 X)
- Google Chrome (리콜 모드는 Selenium으로 Chrome을 직접 띄웁니다)
- 아래 패키지 설치:

```bash
pip install -r requirements.txt (일반사용자는 X)
```

## 사용법

### 1. 단어 추출 북마크릿 등록

크롬 북마크 바에 새 북마크를 만들고, 이름은 아무거나(예: `단어추출`), URL 칸에 아래 코드를 통째로 붙여넣으세요.

```
javascript:(function(){try{let sets=[];document.querySelectorAll('.flip-card').forEach(card=>{let eng=card.querySelector('.ex_front')?.innerText.trim();let kor=card.querySelector('.ex_back')?.innerText.trim();if(eng&&kor){sets.push({eng:eng,kor:kor});}});if(sets.length===0){alert('단어를 찾지 못했습니다. (0개) 페이지 구조가 다를 수 있어요.');return;}let jsonText=JSON.stringify(sets);function fallbackCopy(text){let ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.focus();ta.select();let ok=false;try{ok=document.execCommand('copy');}catch(e){ok=false;}document.body.removeChild(ta);return ok;}window.focus();navigator.clipboard.writeText(jsonText).then(()=>{alert('단어 '+sets.length+'개가 클립보드에 복사되었습니다!');}).catch(()=>{if(fallbackCopy(jsonText)){alert('단어 '+sets.length+'개가 클립보드에 복사되었습니다! (대체 방식)');}else{alert('클립보드 복사에 실패했습니다. (단어 '+sets.length+'개는 찾았음)');}});}catch(e){alert('오류 발생: '+e.message);}})();
```

### 2. 프로그램 실행

```bash
python main.py (일반사용자는 X)
```

`📋 클립보드 불러오기` 버튼을 누르기 전에, 클래스카드 단어장 페이지에서 위 북마크릿을 먼저 클릭해 단어를 클립보드에 복사해두세요.

### 3-A. 암기 학습 모드

1. `클립보드 불러오기` → `학습창 클릭` 클릭
2. 10초 안에 암기 학습창의 카드 중앙으로 마우스를 이동
3. `암기 시작` 클릭 → 자동으로 카드가 넘어갑니다
4. 중단하려면 ` 정지` 또는 `Esc`

### 3-B. 리콜 학습 모드

1. `클립보드 불러오기` → '리콜 자동 풀이 시작` 클릭
2. 자동으로 열리는 Chrome에서 로그인 후 리콜 학습 화면으로 이동
3. 문제가 나타나면 자동으로 정답을 찾아 선택합니다
4. 중단하려면 `정지` 또는 `Esc`

## 배포용 설치 프로그램 만들기

1. PyInstaller로 실행 파일 빌드:
   ```bash
   pyinstaller --onefile --windowed --name "암기" main.py
   ```
   빌드 결과물(`dist/암기.exe`)을 `setup.iss`와 같은 폴더로 복사합니다.
2. [Inno Setup](https://jrsoftware.org/isinfo.php)으로 `setup.iss`를 열어 컴파일하면 `Classcard_Setup.exe` 설치 프로그램이 만들어집니다. (일반사용자는 X)

## 주의사항

- 개인 학습 보조 목적의 자동화 도구입니다. 사용은 본인 책임하에 진행해주세요.
- 클래스카드 사이트의 DOM 구조가 바뀌면 `main.py` 안의 CSS 셀렉터(`.normal-body`, `.cc-ellipsis` 등)를 다시 맞춰야 할 수 있습니다.
- 이 경우 이슈에 올려주시면 빠르게 처리하겠습니다.

## License

이 프로젝트는 LICENSE 파일에 명시된 라이선스(Apache License 2.0)를 따릅니다.
