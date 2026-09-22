# 클래스카드 매크로

클래스카드(classcard.net) 단어장의 **암기 / 리콜 / 스펠 / 테스트**를 자동으로 풀어주는 Windows 프로그램입니다.

---

## 📥 설치하기

1. 이 페이지 오른쪽의 [**Releases**](../../releases)를 누릅니다.
2. 맨 위 버전에서 **`Classcard_Setup.exe`** 를 내려받습니다.
3. 받은 파일을 실행해서 설치합니다. (다음 → 다음 → 설치)
4. 바탕화면에 생긴 **클래스카드 매크로**를 실행합니다.

> **Google Chrome**이 컴퓨터에 설치되어 있어야 합니다. 다른 건 따로 설치할 필요 없습니다.

---

## 🔖 처음 한 번만: 단어 복사 북마크 만들기

프로그램이 정답을 알려면 단어장의 단어 목록이 필요합니다. 이걸 한 번에 복사해 주는 **북마크**를 만들어 둡니다.

1. 크롬에서 `Ctrl + Shift + B`를 눌러 **북마크바**를 켭니다.
2. 북마크바 빈 곳에 마우스 오른쪽 클릭 → **페이지 추가**
3. **이름**: `단어추출` (아무거나 괜찮아요)
4. **URL**: 아래 코드를 **전부** 복사해서 붙여넣고 저장합니다.

```
javascript:(function(){try{let sets=[];document.querySelectorAll('.flip-card').forEach(card=>{let eng=card.querySelector('.ex_front')?.innerText.trim();let kor=card.querySelector('.ex_back')?.innerText.trim();if(eng&&kor){sets.push({eng:eng,kor:kor});}});if(sets.length===0){alert('단어를 찾지 못했습니다. (0개) 페이지 구조가 다를 수 있어요.');return;}let jsonText=JSON.stringify(sets);function fallbackCopy(text){let ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.focus();ta.select();let ok=false;try{ok=document.execCommand('copy');}catch(e){ok=false;}document.body.removeChild(ta);return ok;}window.focus();navigator.clipboard.writeText(jsonText).then(()=>{alert('단어 '+sets.length+'개가 클립보드에 복사되었습니다!');}).catch(()=>{if(fallbackCopy(jsonText)){alert('단어 '+sets.length+'개가 클립보드에 복사되었습니다! (대체 방식)');}else{alert('클립보드 복사에 실패했습니다. (단어 '+sets.length+'개는 찾았음)');}});}catch(e){alert('오류 발생: '+e.message);}})();
```

---

## ▶️ 사용법

### 0단계: 단어 / 문장 고르기 (매번)

프로그램을 켜면 왼쪽 위 **`단어`** / **`문장`** 중 하나를 먼저 눌러야 다른 버튼을 쓸 수 있습니다. (선택된 쪽이 빨간색이 됩니다)

- 영어 단어장이면 **`단어`**
- 문장(본문) 세트면 **`문장`**

### 1단계: 단어 불러오기 (매번)

1. 클래스카드에서 공부할 **단어장 페이지**(단어 목록이 보이는 곳)를 엽니다.
2. 북마크바의 **`단어추출`** 을 누릅니다. → "단어 ○개가 클립보드에 복사되었습니다!" 가 뜨면 성공
3. 프로그램에서 **`클립보드 불러오기`** 를 누릅니다.

### 2단계: 원하는 모드 실행

모드 버튼을 누르면 **자동 조작용 크롬 창**이 새로 뜹니다. 그 창에서 로그인하고 학습 화면으로 들어가면 됩니다.
(한 번 로그인하면 다른 모드도 같은 창을 계속 씁니다.)

| 모드 | 이렇게 하세요 |
|---|---|
| **암기** | `암기 시작` → 크롬에서 암기 학습 화면으로 이동 → 오른쪽 **`시작`** 버튼이 켜지면 누르기 |
| **리콜** | `리콜 자동 풀이 시작` → 크롬에서 리콜 학습 화면으로 이동 → 자동으로 정답을 골라줍니다 |
| **스펠** | `스펠 자동 풀이 시작` → 크롬에서 스펠 학습 화면으로 이동 → 자동으로 뜻을 입력합니다 |
| **테스트** | `테스트 자동 풀이 시작` → 크롬에서 테스트 화면으로 이동 → 자동으로 정답을 골라줍니다 |

> 💡 **학습 방법은 '단어제시'로 해주세요.** 암기·리콜·스펠 모드는 '단어제시' 방식에 맞춰져 있습니다.

**문장 모드**에서는 네 모드 모두 낱말 조각을 순서대로 눌러 문장을 완성합니다.

| 모드 | 이렇게 하세요 |
|---|---|
| **암기 (영작 연습)** | `영작 연습 시작` → 크롬에서 암기 학습 화면으로 이동 → 자동으로 문장을 배열합니다 |
| **리콜 (듣고 배열)** | `리콜 자동 풀이 시작` → 리콜 학습 화면으로 이동 |
| **스펠 (배열)** | `스펠 자동 풀이 시작` → 스펠 학습 화면으로 이동 |
| **테스트 (어순배열)** | `테스트 자동 풀이 시작` → 테스트 화면에서 **직접 `테스트 시작`을 누르면** 자동으로 풀고 `다음 문제`로 넘어갑니다 |

> 💡 **문장 모드의 스펠 학습은 '어순배열' 방식에 최적화되어 있습니다.**

### 멈추기 / 다시 시작

- **멈추기**: 프로그램 맨 아래 **`정지`** 버튼 (또는 프로그램 창에서 `Esc` 키)
- **이상하게 멈췄을 때**: 오른쪽 위 **`재시작`** 버튼 → 프로그램만 다시 켜집니다. 크롬 창과 로그인은 그대로 남아 있어요.
- 다른 모드로 바꾸고 싶으면: 크롬에서 다른 학습 화면으로 이동한 뒤 그 모드 버튼만 누르면 됩니다.

---

## ❓ 자주 묻는 질문

- **"단어를 찾지 못했습니다 (0개)"가 떠요** → 학습 화면이 아니라 **단어 목록이 보이는 단어장 페이지**에서 북마크를 눌러야 합니다.
- **정답을 못 고르고 가만히 있어요** → 단어를 그 단어장에서 불러왔는지 확인하세요. 다른 단어장의 단어를 불러오면 정답을 모릅니다.
- **크롬 창을 닫아버렸어요** → 모드 버튼을 다시 누르면 새 창이 뜹니다.
- **잘 안 되는 부분이 있어요** → [Issues](../../issues)에 어떤 모드에서 어떻게 안 되는지 올려주세요.

## ⚠️ 주의사항

- 개인 학습 보조용입니다. 사용은 본인 책임입니다.
- 클래스카드 사이트가 바뀌면 일부 모드가 동작하지 않을 수 있습니다. 이 경우 [Issues](../../issues)에 알려주시면 빠르게 고치겠습니다.

---
---

## 🛠 개발자용 안내

> 여기부터는 소스코드를 직접 실행하거나 수정하려는 분을 위한 내용입니다. 일반 사용자는 볼 필요 없습니다.

### 구조

- Python(Tkinter) GUI + Selenium(Chrome 자동 조작). 코드는 전부 `main.py` 하나에 있습니다.
- 모든 모드가 Chrome 창 하나를 공유합니다. Chrome은 `--remote-debugging-port=9333`으로 띄우고, 프로그램을 재시작하면 그 포트로 기존 창에 다시 연결합니다.
- 단어 데이터는 위 북마크릿이 만든 JSON(`[{"eng": ..., "kor": ...}, ...]`)을 클립보드에서 읽습니다.

### 소스에서 실행

필요한 것: Python 3.x, Google Chrome

```bash
pip install -r requirements.txt
```

```bash
python main.py
```

### 설치 파일 만들기

1. PyInstaller로 실행 파일 빌드:
   ```bash
   pyinstaller --onefile --windowed --name "암기" --collect-all selenium main.py
   ```
   `--collect-all selenium`은 꼭 넣어야 합니다. 빼면 selenium의 지연 임포트 모듈과 `selenium-manager.exe`가 빠져서, 빌드는 되어도 실행 중에 selenium 에러가 납니다.
2. `dist/암기.exe`를 `setup.iss`와 같은 폴더로 복사합니다.
3. [Inno Setup](https://jrsoftware.org/isinfo.php)으로 `setup.iss`를 컴파일하면 `Classcard_Setup.exe`가 만들어집니다.

### 사이트 구조가 바뀌었을 때

`main.py` 안의 CSS 셀렉터를 다시 맞춰야 할 수 있습니다. 주요 셀렉터: `.normal-body`, `.cc-ellipsis`(리콜), `.spell-content`, `input[name="input_answer"]`(스펠), `.cc-table.middle.fill-parent`(테스트).

## License

이 프로젝트는 LICENSE 파일에 명시된 라이선스(Apache License 2.0)를 따릅니다.
