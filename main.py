import ctypes
import json
import threading
import time
import tkinter as tk
from tkinter import messagebox
import pyautogui
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# --- DPI 및 PyAutoGUI 설정 ---
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.03

try:
  ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
  try:
    ctypes.windll.user32.SetProcessDPIAware()
  except Exception:
    pass

is_running = False
word_list = []
target_click_pos = None


# --- 클립보드에서 데이터 바로 읽기 ---
def load_from_clipboard():
  global word_list
  word_list.clear()

  try:
    raw_text = root.clipboard_get().strip()
    data = json.loads(raw_text)

    if isinstance(data, list) and len(data) > 0 and 'eng' in data[0]:
      word_list = data
      lbl_status.config(
          text=(
              f'준비 완료! 총 {len(word_list)}개 단어 로드됨\n모드에 맞는 버튼을'
              ' 누르세요.'
          ),
          fg='#388E3C',
      )
      btn_pos.config(state=tk.NORMAL)
      btn_recall_start.config(state=tk.NORMAL)
    else:
      messagebox.showerror('에러', '올바른 클래스카드 데이터가 아닙니다.')
  except Exception:
    messagebox.showerror(
        '에러',
        '클립보드에 데이터가 없습니다.\n먼저 브라우저 북마크를 눌러주세요.',
    )


# --- 암기 학습 창 위치 등록 (10초 대기) ---
def set_target_position():
  global target_click_pos

  if not word_list:
    messagebox.showwarning('알림', '먼저 단어를 불러와주세요.')
    return

  lbl_status.config(
      text='10초 내에 암기학습 [카드 중앙]을 이동해 클릭하세요!', fg='#1976D2'
  )
  root.update()
  time.sleep(10)

  target_click_pos = pyautogui.position()
  lbl_status.config(
      text=f'암기 매크로 대기 중 ({len(word_list)}개)\n[▶ 암기 시작]을 누르세요.',
      fg='#388E3C',
  )
  btn_start.config(state=tk.NORMAL)


# --- 암기 학습 루프 ---
def run_macro_loop():
  global is_running, word_list, target_click_pos

  if target_click_pos:
    pyautogui.click(target_click_pos.x, target_click_pos.y)
    time.sleep(0.3)

  total = len(word_list)
  idx = 0

  while is_running and idx < total:
    if not is_running:
      break

    pyautogui.press('space')
    time.sleep(0.2)
    if not is_running:
      break

    pyautogui.hotkey('shift', 'space')
    time.sleep(0.2)
    if not is_running:
      break

    pyautogui.press('right')
    time.sleep(0.3)

    idx += 1

  if is_running:
    root.after(
        0, lambda: lbl_status.config(text='🎉 암기 학습 완료!', fg='#388E3C')
    )
    stop_macro()


def start_macro():
  global is_running
  if not word_list or target_click_pos is None:
    messagebox.showwarning('알림', '단어 데이터 및 위치 지정을 완료해주세요.')
    return

  is_running = True
  lbl_status.config(
      text='🚀 암기 자동 학습 진행 중... ([■ 정지] 버튼 클릭 시 중단)',
      fg='#388E3C',
  )
  btn_load.config(state=tk.DISABLED)
  btn_pos.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  t = threading.Thread(target=run_macro_loop, daemon=True)
  t.start()


# --- 리콜 학습 자동 풀이 스레드 ---
def run_recall_selenium():
  if not word_list:
    messagebox.showwarning('알림', '먼저 [📋 불러오기]로 단어를 로드해주세요.')
    return

  t = threading.Thread(target=selenium_worker, daemon=True)
  t.start()


_VISIBLE_JS = """
function isReallyVisible(el) {
  if (typeof el.checkVisibility === 'function') {
    try {
      return el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
    } catch (e) {}
  }
  var node = el;
  while (node && node.nodeType === 1) {
    var cs = getComputedStyle(node);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) {
      return false;
    }
    node = node.parentElement;
  }
  var r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
var el = arguments[0];
return {text: (el.textContent || '').trim(), visible: isReallyVisible(el)};
"""


def read_visible(driver, selector):
  """checkVisibility()(구형 Chrome 폴백: 조상 체인의 display/visibility/
  opacity를 직접 훑음)로 '진짜 화면에 렌더링된' 요소만 걸러 (element, text)
  목록을 반환한다. 이전에 썼던 elementFromPoint 히트테스트 방식은, 순수
  텍스트 라벨에 흔히 걸리는 pointer-events:none 때문에 브라우저가 그 지점의
  히트테스트에서 해당 요소를 건너뛰고 엉뚱한 요소를 반환해버리는 문제가
  있었다. checkVisibility는 히트테스트가 아니라 렌더링 여부만 보므로 이
  문제에 영향받지 않는다. 이렇게 걸러내면 DOM에 잔여/미래 문제의 요소가
  아무리 많이 쌓여있어도(.cc-ellipsis 91개 확인됨) 실제로 지금 화면에 보이는
  것만 남는다."""
  try:
    els = driver.find_elements(By.CSS_SELECTOR, selector)
  except Exception:
    return []
  results = []
  for el in els:
    try:
      info = driver.execute_script(_VISIBLE_JS, el)
    except StaleElementReferenceException:
      continue
    except Exception:
      continue
    if info.get('visible') and info.get('text'):
      results.append((el, info['text']))
  return results


def selenium_worker():
  global is_running, word_list
  is_running = True

  root.after(
      0,
      lambda: lbl_status.config(
          text='🌐 크롬 실행 중... 로그인 후 리콜 학습에 들어가세요',
          fg='#1976D2',
      ),
  )

  btn_load.config(state=tk.DISABLED)
  btn_pos.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  QUESTION_SELECTOR = '.normal-body'
  CHOICE_SELECTOR = '.cc-ellipsis'  # l1/l2 등 줄수 클래스는 제외하고 공통 클래스만 사용

  try:
    options = webdriver.ChromeOptions()
    options.add_experimental_option('detach', True)
    driver = webdriver.Chrome(options=options)
    driver.get('https://www.classcard.net')

    root.after(
        0,
        lambda: lbl_status.config(
            text='👀 리콜 학습 화면 감지 대기 중...', fg='#2E7D32'
        ),
    )

    last_seen_eng = None
    last_logged_qtext = None

    while is_running:
      current_word = None

      # 1) 화면에 실제로 렌더링된 문제 요소만 읽는다(checkVisibility 기반).
      #    혹시 판정이 어긋나 잔여 요소가 섞여 들어와도, "단어장에 있으면서
      #    last_seen_eng과 다른" 조건을 안전장치로 함께 건다.
      q_visible = read_visible(driver, QUESTION_SELECTOR)
      q_candidates = [t for _, t in q_visible]

      if q_candidates != last_logged_qtext:
        print(f"[DEBUG] 화면에 실제로 보이는 문제 텍스트들={q_candidates}")
        last_logged_qtext = q_candidates

      for cand in q_candidates:
        cand_lower = cand.strip().lower()
        for w in word_list:
          if w['eng'].strip().lower() == cand_lower and w['eng'] != last_seen_eng:
            current_word = w
            break
        if current_word:
          break

      if current_word and is_running:
        target_kor = current_word['kor'].strip()
        msg = f"🔍 단어 감지: [{current_word['eng']}] -> 정답 뜻: '{target_kor}'"
        root.after(0, lambda m=msg: lbl_status.config(text=m, fg='#0288D1'))

        time.sleep(0.5)  # 카드 전환 애니메이션 시작 대기

        # 화면에 실제로 보이는 보기만 최대 2초 재시도하며 읽는다.
        choices, attempt = [], 0
        for attempt in range(20):
          if not is_running:
            break
          choices = read_visible(driver, CHOICE_SELECTOR)
          if len(choices) >= 4:
            break
          time.sleep(0.1)

        debug_texts = [t for _, t in choices]
        print(
            f"[DEBUG] target_kor='{target_kor}' / 화면에 보이는 보기들={debug_texts} "
            f"(시도={attempt + 1}회)"
        )

        pressed = False
        for i, (choice_el, txt) in enumerate(choices[:4]):
          if not is_running:
            break
          if txt and target_kor in txt:
            try:
              body = driver.find_element(By.TAG_NAME, 'body')
              body.click()
              time.sleep(0.3)
              ActionChains(driver).send_keys(str(i + 1)).perform()
              pressed = True
            except StaleElementReferenceException:
              pass
            except Exception as e:
              print('키 입력 에러:', e)
            break
        print(f"[DEBUG] 매칭성공={pressed}")

        if pressed and is_running:
          last_seen_eng = current_word['eng']
          root.after(
              0,
              lambda t=target_kor: lbl_status.config(
                  text=f'✨ 정답 [{t}] 선택 성공! 다음 문제 이동',
                  fg='#388E3C',
              ),
          )
          time.sleep(0.4)
      else:
        root.after(
            0,
            lambda: lbl_status.config(
                text='👀 리콜 학습 화면을 기다리는 중...', fg='gray'
            ),
        )

      time.sleep(0.3)

  except Exception as e:
    err_msg = str(e)
    root.after(
        0,
        lambda: messagebox.showerror(
            '셀레니움 에러', f'오류가 발생했습니다:\n{err_msg}'
        ),
    )

  if is_running:
    stop_macro()


def stop_macro(event=None):
  global is_running
  is_running = False
  lbl_status.config(text='정지됨', fg='#D32F2F')
  btn_load.config(state=tk.NORMAL)
  btn_pos.config(state=tk.NORMAL)
  btn_start.config(state=tk.NORMAL if target_click_pos else tk.DISABLED)
  btn_recall_start.config(state=tk.NORMAL)
  btn_stop.config(state=tk.DISABLED)


# --- UI 구성 ---
root = tk.Tk()
root.title('클래스카드 매크로')
root.geometry('480x290')
root.resizable(True, True)
root.wm_attributes('-topmost', True)

root.bind('<Escape>', stop_macro)

lbl_status = tk.Label(
    root,
    text='1. 북마크 추출 -> 2. [📋 불러오기] -> 모드 선택',
    font=('맑은 고딕', 9),
    fg='gray',
    wraplength=460,
    justify='center',
)
lbl_status.pack(pady=(15, 10))

# 공통 불러오기 버튼
frame_top = tk.Frame(root)
frame_top.pack(pady=5)
btn_load = tk.Button(
    frame_top,
    text='📋 클립보드 불러오기',
    width=30,
    font=('맑은 고딕', 10, 'bold'),
    fg='#0288D1',
    command=load_from_clipboard,
)
btn_load.pack()

# 암기 모드 컨트롤 프레임
frame_memo = tk.LabelFrame(
    root, text=' 📖 암기 학습 모드 ', font=('맑은 고딕', 9, 'bold')
)
frame_memo.pack(pady=8, padx=15, fill='x')

btn_pos = tk.Button(
    frame_memo,
    text='🎯 학습창 클릭',
    width=15,
    font=('맑은 고딕', 9, 'bold'),
    fg='#E65100',
    command=set_target_position,
    state=tk.DISABLED,
)
btn_pos.pack(side=tk.LEFT, padx=12, pady=8)

btn_start = tk.Button(
    frame_memo,
    text='▶ 암기 시작',
    width=15,
    font=('맑은 고딕', 9, 'bold'),
    fg='#2E7D32',
    command=start_macro,
    state=tk.DISABLED,
)
btn_start.pack(side=tk.LEFT, padx=12, pady=8)

# 리콜 학습 모드 컨트롤 프레임
frame_recall = tk.LabelFrame(
    root, text=' 🧪 리콜 학습 모드 ', font=('맑은 고딕', 9, 'bold')
)
frame_recall.pack(pady=8, padx=15, fill='x')

btn_recall_start = tk.Button(
    frame_recall,
    text='🧪 리콜 자동 풀이 시작',
    width=23,
    font=('맑은 고딕', 10, 'bold'),
    fg='#6A1B9A',
    command=run_recall_selenium,
    state=tk.DISABLED,
)
btn_recall_start.grid(row=0, column=0, padx=12, pady=8)

btn_stop = tk.Button(
    frame_recall,
    text='■ 정지',
    width=8,
    font=('맑은 고딕', 10, 'bold'),
    fg='#C62828',
    command=stop_macro,
    state=tk.DISABLED,
)
btn_stop.grid(row=0, column=1, padx=12, pady=8)

root.mainloop()
