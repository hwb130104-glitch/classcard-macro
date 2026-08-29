import ctypes
import json
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox
import pyautogui
# selenium 4.28+ 는 webdriver.Chrome / webdriver.ChromeOptions 를 importlib
# 기반 지연 임포트로 노출한다. PyInstaller가 정적 분석으로 이걸 못 찾아서
# exe로 빌드하면 실행 중에 "No module named
# 'selenium.webdriver.chrome.options'" 에러가 났다. 실제 모듈을 직접
# 임포트해두면 빌드에도 확실히 포함된다.
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.webdriver import WebDriver as ChromeDriver
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
shared_driver = None


# --- 모드 전환마다 새 크롬을 띄우지 않고 하나를 계속 재사용 ---
def get_driver():
  global shared_driver

  if shared_driver is not None:
    try:
      shared_driver.current_url  # 창이 아직 살아있는지 확인
      return shared_driver
    except Exception:
      shared_driver = None  # 창이 닫혔으면 새로 띄운다

  options = ChromeOptions()
  options.add_experimental_option('detach', True)
  shared_driver = ChromeDriver(options=options)
  shared_driver.get('https://www.classcard.net')
  return shared_driver


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
      btn_start.config(state=tk.NORMAL)
      btn_recall_start.config(state=tk.NORMAL)
      btn_spell_start.config(state=tk.NORMAL)
      btn_test_start.config(state=tk.NORMAL)
    else:
      messagebox.showerror('에러', '올바른 클래스카드 데이터가 아닙니다.')
  except Exception:
    messagebox.showerror(
        '에러',
        '클립보드에 데이터가 없습니다.\n먼저 브라우저 북마크를 눌러주세요.',
    )


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
        0, lambda: lbl_status.config(text='암기 학습 완료!', fg='#388E3C')
    )
    stop_macro()


def start_macro():
  global is_running, target_click_pos
  if not word_list:
    messagebox.showwarning('알림', '먼저 단어를 불러와주세요.')
    return

  # 버튼을 누른 순간 포커스는 이 프로그램(Tkinter 창)에 가있어서, 그대로
  # 키 입력을 보내면 학습창이 아니라 여기로 전달된다. 5초 안에 학습창의
  # 카드 중앙으로 마우스를 이동시켜두면 그 위치를 기억했다가 매크로 시작
  # 직전에 실제로 클릭해서 포커스를 넘긴다.
  lbl_status.config(
      text='5초 내에 암기학습 [카드 중앙]으로 마우스를 이동하세요!', fg='#1976D2'
  )
  root.update()
  time.sleep(5)

  target_click_pos = pyautogui.position()

  is_running = True
  lbl_status.config(
      text='암기 자동 학습 진행 중... ([정지] 버튼 클릭 시 중단)',
      fg='#388E3C',
  )
  btn_load.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_spell_start.config(state=tk.DISABLED)
  btn_test_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  t = threading.Thread(target=run_macro_loop, daemon=True)
  t.start()


# --- 리콜 학습 자동 풀이 스레드 ---
def run_recall_selenium():
  if not word_list:
    messagebox.showwarning('알림', '먼저 [불러오기]로 단어를 로드해주세요.')
    return

  t = threading.Thread(target=selenium_worker, daemon=True)
  t.start()


# --- 스펠(뜻 입력) 학습 자동 풀이 스레드 ---
def run_spelling_selenium():
  if not word_list:
    messagebox.showwarning('알림', '먼저 [불러오기]로 단어를 로드해주세요.')
    return

  t = threading.Thread(target=spelling_worker, daemon=True)
  t.start()


# --- 테스트(최종 시험) 학습 자동 풀이 스레드 ---
def run_test_selenium():
  if not word_list:
    messagebox.showwarning('알림', '먼저 [불러오기]로 단어를 로드해주세요.')
    return

  t = threading.Thread(target=test_worker, daemon=True)
  t.start()


def _strip_pos_tag(text):
  """'[명] 북극곰' 같은 한글 뜻 문자열에서 앞의 품사 태그를 떼고
  '북극곰'만 남긴다. 테스트 모드 보기 박스는 태그 없이 뜻만 표시하기
  때문에 비교 시 이 형태와도 맞춰봐야 한다."""
  return re.sub(r'^\[[^\]]+\]\s*', '', text).strip()


def _norm_space(text):
  """앞뒤 공백을 없애고 중간 공백도 한 칸으로 통일한다."""
  return re.sub(r'\s+', ' ', text or '').strip()


def _pick_choice(choices, target):
  """보기 목록에서 정답 보기를 골라 (인덱스, 요소, 텍스트)를 돌려준다.

  단순 포함(in) 비교만 하면 '[형] 신'을 찾을 때 위에 있는 '[형] 신축성
  있는'이 먼저 걸려서 오답을 골랐다(리콜/테스트 양쪽에서 실제로 발생).
  완전히 같은 것 -> 품사 태그만 뗀 것이 같은 것 -> 마지막 수단으로 포함
  관계 순으로 단계를 나눠서, 정확한 보기가 있으면 항상 그쪽을 고른다."""
  target_norm = _norm_space(target)
  target_stripped = _strip_pos_tag(target_norm)
  items = []
  for idx, (el, raw) in enumerate(choices):
    txt = _norm_space(raw)
    if txt:
      items.append((idx, el, txt, _strip_pos_tag(txt)))

  for idx, el, txt, txt_stripped in items:
    if txt == target_norm:
      return idx, el, txt
  for idx, el, txt, txt_stripped in items:
    if target_stripped and txt_stripped == target_stripped:
      return idx, el, txt
  for idx, el, txt, txt_stripped in items:
    if target_norm in txt or txt in target_norm:
      return idx, el, txt
  return None


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


_FIND_INPUT_NEAR_JS = """
var el = arguments[0];
var node = el;
for (var levels = 0; levels < 8 && node; levels++) {
  var inputs = node.querySelectorAll('input[name="input_answer"]');
  for (var i = 0; i < inputs.length; i++) {
    var r = inputs[i].getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      return inputs[i];
    }
  }
  node = node.parentElement;
}
return null;
"""


_DISPATCH_KEY_JS = """
var key = arguments[0];
var opts = {key: key, code: key === ' ' ? 'Space' : 'Digit' + key,
  keyCode: key === ' ' ? 32 : key.charCodeAt(0),
  which: key === ' ' ? 32 : key.charCodeAt(0),
  bubbles: true, cancelable: true};
document.activeElement.dispatchEvent(new KeyboardEvent('keydown', opts));
document.activeElement.dispatchEvent(new KeyboardEvent('keypress', opts));
document.activeElement.dispatchEvent(new KeyboardEvent('keyup', opts));
"""


def dispatch_key(driver, key):
  """ActionChains의 OS 레벨 키 입력이 씹히는 경우를 대비해, JS로 직접
  keydown/keypress/keyup 이벤트를 발생시키는 대체 경로. 두 방식을 함께
  쓰면 한쪽이 안 먹혀도 다른 쪽으로 전달될 가능성이 높아진다."""
  try:
    driver.execute_script(_DISPATCH_KEY_JS, key)
  except Exception:
    pass


_SECTION_DONE_JS = r"""
// 구간이 끝나면 "GOOD JOB!! / 구간 학습이 완료되었습니다" 화면이 뜨는데,
// 여기엔 문제도 보기도 없어서 매크로가 그대로 멈춰 있었다. 화면에 보이는
// [다음 구간으로 이동] 버튼을 찾아서 돌려준다. 텍스트가 정확히 일치하는
// 것만 보므로, 이 문구를 품고 있는 바깥 div가 잘못 걸리지 않는다.
var wanted = arguments[0];
var nodes = document.querySelectorAll('a, button, input[type="button"], div, span, p');
for (var i = 0; i < nodes.length; i++) {
  var el = nodes[i];
  var t = (el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  if (wanted.indexOf(t) === -1) continue;
  if (typeof el.checkVisibility === 'function') {
    try {
      if (!el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    } catch (e) {}
  }
  var r = el.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) return el;
}
return null;
"""

_SECTION_DONE_LABELS = ['다음 구간으로 이동', '다음 구간 이동', '계속하기']


def handle_section_done(driver):
  """구간 완료 화면이면 다음 구간으로 넘기고 True를 돌려준다.

  버튼 클릭이 막히는 경우를 대비해 스페이스 입력도 함께 시도한다(화면에
  SPACE 안내가 붙어 있다)."""
  try:
    btn = driver.execute_script(_SECTION_DONE_JS, _SECTION_DONE_LABELS)
  except Exception:
    return False

  if not btn:
    return False

  print('[DEBUG] 구간 완료 화면 감지 - 다음 구간으로 이동')
  moved = False
  try:
    btn.click()
    moved = True
  except Exception:
    pass

  if not moved:
    try:
      driver.find_element(By.TAG_NAME, 'body').click()
      time.sleep(0.15)
      ActionChains(driver).send_keys(Keys.SPACE).perform()
      dispatch_key(driver, ' ')
      moved = True
    except Exception as e:
      print('구간 이동 에러:', e)

  if moved:
    time.sleep(1.2)  # 다음 구간 첫 화면이 뜰 때까지 잠시 대기
  return moved


def read_all_texts(driver, selector):
  """selector에 매칭되는 모든 요소의 textContent를 가시성 판단 없이 그대로
  반환한다. 일부 요소는 checkVisibility 기반 판정(read_visible)이 두 번째
  문제부터 계속 실패하는 경우가 있었는데, 그때는 가시성 판단 자체를 빼고
  "단어장에 있으면서 방금 처리한 것과 다르면 새 문제"로 구분하는 방식이
  안정적이었다."""
  try:
    els = driver.find_elements(By.CSS_SELECTOR, selector)
  except Exception:
    return []
  texts = []
  for el in els:
    try:
      t = driver.execute_script("return (arguments[0].textContent || '').trim();", el)
    except StaleElementReferenceException:
      t = ''
    except Exception:
      t = ''
    if t:
      texts.append(t)
  return texts


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
          text='브라우저 준비 중... 리콜 학습 화면으로 이동하세요',
          fg='#1976D2',
      ),
  )

  btn_load.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_spell_start.config(state=tk.DISABLED)
  btn_test_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  QUESTION_SELECTOR = '.normal-body'
  CHOICE_SELECTOR = '.cc-ellipsis'  # l1/l2 등 줄수 클래스는 제외하고 공통 클래스만 사용

  try:
    driver = get_driver()

    root.after(
        0,
        lambda: lbl_status.config(
            text='리콜 학습 화면 감지 대기 중...', fg='#2E7D32'
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
        msg = f"단어 감지: [{current_word['eng']}] -> 정답 뜻: '{target_kor}'"
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
        picked = _pick_choice(choices[:4], target_kor)
        if picked and is_running:
          i, _, matched_text = picked
          print(f"[DEBUG] 고른 보기={i + 1}번 '{matched_text}'")
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
        print(f"[DEBUG] 매칭성공={pressed}")

        if pressed and is_running:
          last_seen_eng = current_word['eng']
          root.after(
              0,
              lambda t=target_kor: lbl_status.config(
                  text=f'정답 [{t}] 선택 성공! 다음 문제 이동',
                  fg='#388E3C',
              ),
          )
          time.sleep(0.4)
      else:
        # 구간 완료 화면이면 여기서 다음 구간으로 넘어간다.
        if handle_section_done(driver):
          continue
        root.after(
            0,
            lambda: lbl_status.config(
                text='리콜 학습 화면을 기다리는 중...', fg='gray'
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


def spelling_worker():
  global is_running, word_list
  is_running = True

  root.after(
      0,
      lambda: lbl_status.config(
          text='브라우저 준비 중... 스펠 학습 화면으로 이동하세요',
          fg='#1976D2',
      ),
  )

  btn_load.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_spell_start.config(state=tk.DISABLED)
  btn_test_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  QUESTION_SELECTOR = '.spell-content'  # F12로 확인한 스펠 모드 전용 문제 표시 요소
  # input의 class 안 임의 토큰(예: aW5wdXQxNzg3NjU5MDI1)은 세션마다 랜덤하게
  # 바뀌어 쓸 수 없고, name="input_answer"가 F12로 확인한 안정적인 속성이다.
  # 실제 검색은 _FIND_INPUT_NEAR_JS에서 문제 요소 기준으로 스코핑해 수행한다.

  try:
    driver = get_driver()

    root.after(
        0,
        lambda: lbl_status.config(
            text='스펠 학습 화면 감지 대기 중...', fg='#2E7D32'
        ),
    )

    last_seen_eng = None
    last_logged_qtext = None

    while is_running:
      current_word = None
      current_word_el = None

      # 화면에 실제로 렌더링된 문제 요소만 읽고, "단어장에 있으면서
      # last_seen_eng과 다른" 것만 새 문제로 인정한다 (리콜 모드와 동일한
      # 검증된 방식 — 가시성 판단 자체가 필요 없어 안정적이다).
      q_visible = read_visible(driver, QUESTION_SELECTOR)
      q_candidates = [t for _, t in q_visible]

      if q_candidates != last_logged_qtext:
        print(f"[DEBUG] 화면에 실제로 보이는 문제 텍스트들={q_candidates}")
        last_logged_qtext = q_candidates

      for el, cand in q_visible:
        cand_lower = cand.strip().lower()
        for w in word_list:
          if w['eng'].strip().lower() == cand_lower and w['eng'] != last_seen_eng:
            current_word = w
            current_word_el = el
            break
        if current_word:
          break

      if current_word and is_running:
        target_kor = current_word['kor'].strip()
        msg = f"단어 감지: [{current_word['eng']}] -> 입력할 뜻: '{target_kor}'"
        root.after(0, lambda m=msg: lbl_status.config(text=m, fg='#0288D1'))

        # input[name="input_answer"]가 이전 문제들의 잔여 요소까지 DOM에
        # 수십 개 누적돼 있어(.cc-ellipsis와 동일한 문제, 실측 62개) 전역
        # 검색+가시성 판단으로는 어떤 게 지금 카드의 입력창인지 구분할 수
        # 없었다. 대신 이미 화면에 보인다고 검증된 문제 요소(current_word_el)
        # 를 기준점 삼아, 그 조상 안에서만 input을 찾도록 스코핑한다.
        input_el, attempt = None, 0
        for attempt in range(20):
          if not is_running:
            break
          try:
            input_el = driver.execute_script(_FIND_INPUT_NEAR_JS, current_word_el)
          except StaleElementReferenceException:
            input_el = None
          if input_el:
            break
          time.sleep(0.1)

        print(
            f"[DEBUG] target_kor='{target_kor}' / 입력창 찾음={input_el is not None} "
            f"(시도={attempt + 1}회)"
        )

        typed = False
        if input_el:
          try:
            input_el.click()
            input_el.clear()
            input_el.send_keys(target_kor)
            time.sleep(0.2)
            input_el.send_keys(Keys.RETURN)
            time.sleep(1.7)  # 정답 확인 후 결과 화면이 뜰 시간
            ActionChains(driver).send_keys(Keys.SPACE).perform()
            typed = True
          except StaleElementReferenceException:
            pass
          except Exception as e:
            print('입력 에러:', e)
        print(f"[DEBUG] 입력성공={typed}")

        if typed and is_running:
          last_seen_eng = current_word['eng']
          root.after(
              0,
              lambda t=target_kor: lbl_status.config(
                  text=f"'{t}' 입력 완료! 다음 문제 이동",
                  fg='#388E3C',
              ),
          )
          time.sleep(0.4)
      else:
        # 구간 완료 화면이면 여기서 다음 구간으로 넘어간다.
        if handle_section_done(driver):
          continue
        root.after(
            0,
            lambda: lbl_status.config(
                text='스펠 학습 화면을 기다리는 중...', fg='gray'
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


def test_worker():
  global is_running, word_list
  is_running = True

  root.after(
      0,
      lambda: lbl_status.config(
          text='브라우저 준비 중... 테스트 학습 화면으로 이동하세요',
          fg='#1976D2',
      ),
  )

  btn_load.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_spell_start.config(state=tk.DISABLED)
  btn_test_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  # F12로 확인: 문제 단어는 텍스트 길이에 따라 폰트 크기 클래스가
  # font-36/font-32처럼 동적으로 바뀐다(길수록 작은 폰트) — 그래서
  # .font-36으로 고정하면 긴 문구에서 아예 매칭이 안 됐다. class 안에
  # "font-"가 들어간 것만 문제, 없는 것만 보기 박스로 구분한다.
  #
  # 또한 뒤로 갈수록 남은 카드들이 미리 렌더링돼 문제 후보가 20개 넘게
  # 한꺼번에 잡히고, 그중 맨 앞 것을 현재 문제로 착각해 계속 실패했다.
  # 카드는 .flip-card로 감싸여 있고 현재 카드만 .next/.hidden이 없으므로
  # (F12 확인: "flip-card showing flip" vs "flip-card next"/"flip-card
  # hidden") 현재 카드 안으로 범위를 좁혀서 찾는다.
  CURRENT_CARD = '.flip-card:not(.next):not(.hidden)'
  FALLBACK_QUESTION_SELECTOR = '.cc-table.middle.fill-parent[class*="font-"]'
  QUESTION_SELECTOR = f'{CURRENT_CARD} {FALLBACK_QUESTION_SELECTOR}'
  CHOICE_SELECTOR = '.cc-table.middle.fill-parent:not([class*="font-"])'

  try:
    driver = get_driver()

    root.after(
        0,
        lambda: lbl_status.config(
            text='테스트 학습 화면 감지 대기 중...', fg='#2E7D32'
        ),
    )

    seen_keys = set()
    fail_counts = {}
    last_logged_qtext = None
    empty_streak = 0

    while is_running:
      current_word = None
      current_cand_norm = None
      direction = None  # 'eng_to_kor' 또는 'kor_to_eng'

      # 문제 진행 중간부터 방향이 뒤집혀(영어->한글 뜻 이었다가 한글 뜻->영어로)
      # 화면에 뜬 텍스트가 word_list의 eng와 일치하는지, kor(태그 뗀 것 포함)와
      # 일치하는지를 매번 판별해서 어느 시점에 바뀌든 대응한다.
      # checkVisibility 판정이 이 요소에서 아주 가끔 실제로 보이는데도
      # 실패하는 경우가 있다(셀렉터 자체는 F12로 확인해 정상 매칭됨). 매번
      # 안 보일 때마다 바로 백업 방식을 쓰면 미래 문제까지 한꺼번에 잡히는
      # 부작용이 있었으므로, 여러 번(약 1.5초) 연속으로 계속 비어있을 때만
      # 백업으로 가시성 판단 없이 원시 텍스트를 읽는다.
      q_visible = read_visible(driver, QUESTION_SELECTOR)
      q_candidates = [t for _, t in q_visible]
      if q_candidates:
        empty_streak = 0
      else:
        empty_streak += 1
        if empty_streak >= 5:
          # 현재 카드 범위로 못 찾으면(카드 클래스 구성이 예상과 다를 때)
          # 마지막 수단으로 범위 제한 없이 다시 찾아본다.
          q_candidates = read_all_texts(driver, QUESTION_SELECTOR)
          if not q_candidates:
            q_candidates = read_all_texts(driver, FALLBACK_QUESTION_SELECTOR)

      if q_candidates != last_logged_qtext:
        print(f"[DEBUG] 화면에 실제로 보이는 문제 텍스트들={q_candidates}")
        last_logged_qtext = q_candidates

      for cand in q_candidates:
        cand_norm = cand.strip()
        if not cand_norm or cand_norm in seen_keys:
          continue
        for w in word_list:
          if w['eng'].strip().lower() == cand_norm.lower():
            current_word = w
            direction = 'eng_to_kor'
            break
          if _strip_pos_tag(w['kor']) == cand_norm or w['kor'].strip() == cand_norm:
            current_word = w
            direction = 'kor_to_eng'
            break
        if current_word:
          current_cand_norm = cand_norm
          break

      if current_word and is_running:
        target_answer = (
            current_word['kor'].strip()
            if direction == 'eng_to_kor'
            else current_word['eng'].strip()
        )
        dir_label = '영단어->한글 뜻' if direction == 'eng_to_kor' else '한글 뜻->영단어'
        msg = f"단어 감지({dir_label}): 정답 '{target_answer}' 찾는 중"
        root.after(0, lambda m=msg: lbl_status.config(text=m, fg='#0288D1'))

        # 단어를 확인한 뒤 바로 스페이스를 눌러 보기 화면으로 넘어간다
        # (이 화면은 "생각할 시간"을 따로 안 기다려도 됨 - 사용자 확인).
        # body.click()으로 매번 포커스를 다시 잡아야 한다 — 포커스가 다른
        # 곳으로 새면 키 입력이 실제 화면에 전혀 반영되지 않은 채로 계속
        # 헛도는 문제가 있었다. 가끔 스페이스 한 번이 안 먹히는 경우가
        # 있어서, 보기가 안 뜨면 스페이스를 다시 눌러보며 최대 3번 시도한다.
        # 문제가 뒤로 갈수록 DOM에 카드가 계속 쌓여 사이트 반응이 점점
        # 느려지는 것으로 보여, 클릭/키 입력 후 대기 시간과 재시도 횟수를
        # 넉넉하게 잡는다.
        choices, attempt = [], 0
        for space_try in range(3):
          if not is_running:
            break
          driver.find_element(By.TAG_NAME, 'body').click()
          time.sleep(0.15)
          ActionChains(driver).send_keys(Keys.SPACE).perform()
          dispatch_key(driver, ' ')
          time.sleep(0.3)

          # 보기 6개가 화면에 실제로 렌더링될 때까지 최대 2.5초 재시도.
          for attempt in range(25):
            if not is_running:
              break
            choices = read_visible(driver, CHOICE_SELECTOR)
            if choices:
              break
            time.sleep(0.1)

          if choices or not is_running:
            break
          print(f"[DEBUG] 보기 화면이 안 떠서 스페이스 재시도 ({space_try + 1}회차)")

        debug_texts = [t for _, t in choices]
        print(
            f"[DEBUG] target_answer='{target_answer}' / 화면에 보이는 보기들={debug_texts} "
            f"(시도={attempt + 1}회)"
        )

        pressed = False
        picked = _pick_choice(choices, target_answer)
        if picked and is_running:
          _, choice_el, matched_text = picked
          print(f"[DEBUG] 고른 보기='{matched_text}'")
          try:
            # 보기 각각은 <label for="radio_0_N">으로 감싸여 있고 N이 곧
            # 눌러야 할 번호다. 위치를 세는 대신 이 속성에서 직접 읽으면
            # DOM에 잔여 요소가 섞여 있어도 정확한 번호를 알 수 있다.
            label_el = choice_el.find_element(By.XPATH, './ancestor::label[1]')
            for_attr = label_el.get_attribute('for') or ''
            digit = for_attr.rsplit('_', 1)[-1]
            if digit.isdigit():
              driver.find_element(By.TAG_NAME, 'body').click()
              time.sleep(0.15)
              ActionChains(driver).send_keys(digit).perform()
              dispatch_key(driver, digit)
              pressed = True
          except StaleElementReferenceException:
            pass
          except Exception as e:
            print('선택 에러:', e)
        print(f"[DEBUG] 매칭성공={pressed}")

        if pressed and is_running:
          fail_counts.pop(current_cand_norm, None)
          seen_keys.add(current_cand_norm)
          root.after(
              0,
              lambda t=target_answer: lbl_status.config(
                  text=f"정답 [{t}] 선택 성공! 다음 문제 이동",
                  fg='#388E3C',
              ),
          )
          time.sleep(0.4)
        elif is_running:
          # 백업 방식(read_all_texts)이 아직 화면에 없는 미래 문제를 잘못
          # 집었을 때, 실패해도 처리 완료로 기록을 안 해두면 매번 같은
          # 틀린 후보를 무한 반복하게 된다. 같은 후보가 몇 번 연속 실패하면
          # 포기하고 넘어가서 다른 후보(진짜 현재 문제)를 시도할 기회를 준다.
          fail_counts[current_cand_norm] = fail_counts.get(current_cand_norm, 0) + 1
          if fail_counts[current_cand_norm] >= 3:
            print(f"[DEBUG] '{current_cand_norm}' 계속 실패해서 포기하고 넘어감")
            seen_keys.add(current_cand_norm)
            fail_counts.pop(current_cand_norm, None)
      else:
        # 구간 완료 화면이면 여기서 다음 구간으로 넘어간다.
        if handle_section_done(driver):
          continue
        root.after(
            0,
            lambda: lbl_status.config(
                text='테스트 학습 화면을 기다리는 중...', fg='gray'
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
  btn_start.config(state=tk.NORMAL if word_list else tk.DISABLED)
  btn_recall_start.config(state=tk.NORMAL)
  btn_spell_start.config(state=tk.NORMAL)
  btn_test_start.config(state=tk.NORMAL)
  btn_stop.config(state=tk.DISABLED)


# --- UI 구성 ---
root = tk.Tk()
root.title('클래스카드 매크로')
root.geometry('480x480')
root.resizable(True, True)
root.wm_attributes('-topmost', True)

root.bind('<Escape>', stop_macro)

lbl_status = tk.Label(
    root,
    text='1. 북마크 추출 -> 2. [불러오기] -> 모드 선택',
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
    text='클립보드 불러오기',
    width=30,
    font=('맑은 고딕', 10, 'bold'),
    fg='#0288D1',
    command=load_from_clipboard,
)
btn_load.pack()

# 암기 모드 컨트롤 프레임
frame_memo = tk.LabelFrame(
    root, text=' 암기 ', font=('맑은 고딕', 9, 'bold')
)
frame_memo.pack(pady=8, padx=15, fill='x')

btn_start = tk.Button(
    frame_memo,
    text='암기 시작',
    width=15,
    font=('맑은 고딕', 9, 'bold'),
    fg='#2E7D32',
    command=start_macro,
    state=tk.DISABLED,
)
btn_start.pack(padx=12, pady=8)

# 리콜 학습 모드 컨트롤 프레임
frame_recall = tk.LabelFrame(
    root, text=' 리콜 ', font=('맑은 고딕', 9, 'bold')
)
frame_recall.pack(pady=8, padx=15, fill='x')

btn_recall_start = tk.Button(
    frame_recall,
    text='리콜 자동 풀이 시작',
    width=23,
    font=('맑은 고딕', 10, 'bold'),
    fg='#6A1B9A',
    command=run_recall_selenium,
    state=tk.DISABLED,
)
btn_recall_start.pack(padx=12, pady=8)

# 스펠(뜻 입력) 학습 모드 컨트롤 프레임
frame_spell = tk.LabelFrame(
    root, text=' 스펠 ', font=('맑은 고딕', 9, 'bold')
)
frame_spell.pack(pady=8, padx=15, fill='x')

btn_spell_start = tk.Button(
    frame_spell,
    text='스펠 자동 풀이 시작',
    width=23,
    font=('맑은 고딕', 10, 'bold'),
    fg='#00695C',
    command=run_spelling_selenium,
    state=tk.DISABLED,
)
btn_spell_start.pack(padx=12, pady=8)

# 테스트(최종 시험) 학습 모드 컨트롤 프레임
frame_test = tk.LabelFrame(
    root, text=' 테스트 ', font=('맑은 고딕', 9, 'bold')
)
frame_test.pack(pady=8, padx=15, fill='x')

btn_test_start = tk.Button(
    frame_test,
    text='테스트 자동 풀이 시작',
    width=23,
    font=('맑은 고딕', 10, 'bold'),
    fg='#AD1457',
    command=run_test_selenium,
    state=tk.DISABLED,
)
btn_test_start.pack(padx=12, pady=8)

# 어느 모드에서든 공통으로 쓰는 정지 버튼 (카테고리 없이 맨 아래 중앙)
btn_stop = tk.Button(
    root,
    text='정지',
    width=10,
    font=('맑은 고딕', 10, 'bold'),
    fg='#C62828',
    command=stop_macro,
    state=tk.DISABLED,
)
btn_stop.pack(pady=(4, 12))

root.mainloop()
