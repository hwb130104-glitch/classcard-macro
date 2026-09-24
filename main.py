import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
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

# --- DPI 설정 ---
try:
  ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
  try:
    ctypes.windll.user32.SetProcessDPIAware()
  except Exception:
    pass

is_running = False
word_list = []
shared_driver = None
# 문장 단어장 모드(왼쪽 위 [단어]/[문장] 전환). 지금은 문장 단어장을 받을 수
# 없어 확인이 안 되므로 숨겨둔다. 다시 쓸 때 True로 바꾸면 된다.
SENTENCE_MODE_ENABLED = True
# 암기: 사용자가 사이트에서 학습을 시작한 뒤 [시작] 버튼을 눌러야 키를 보낸다.
memo_go = threading.Event()


# --- 모드 전환마다 새 크롬을 띄우지 않고 하나를 계속 재사용 ---
# 매크로가 띄운 크롬에 고정 포트로 원격 조종 창구를 열어둔다. 프로그램만
# 재시작했을 때 새 프로그램이 이 포트로 기존 크롬에 다시 붙는다(로그인 유지).
_CHROME_DEBUG_PORT = 9333


def _chrome_debug_port_open():
  """매크로가 띄운 크롬이 아직 살아 있는지 포트로 빠르게 확인한다."""
  try:
    with socket.create_connection(('127.0.0.1', _CHROME_DEBUG_PORT), timeout=0.3):
      return True
  except OSError:
    return False


def get_driver():
  global shared_driver

  if shared_driver is not None:
    try:
      shared_driver.current_url  # 창이 아직 살아있는지 확인
      return shared_driver
    except Exception:
      shared_driver = None  # 창이 닫혔으면 새로 띄운다

  # [재시작]으로 프로그램만 새로 떴다면, 이미 떠 있는 크롬에 다시 붙는다.
  # 포트를 먼저 확인하는 건, 크롬이 없을 때 연결을 시도하면 한참 기다리다
  # 실패하기 때문이다.
  if _chrome_debug_port_open():
    try:
      attach = ChromeOptions()
      attach.add_experimental_option(
          'debuggerAddress', f'127.0.0.1:{_CHROME_DEBUG_PORT}'
      )
      shared_driver = ChromeDriver(options=attach)
      _keep_page_awake(shared_driver)
      print('[DEBUG] 기존 크롬 창에 다시 연결')
      return shared_driver
    except Exception as e:
      print(f'[DEBUG] 기존 크롬 연결 실패, 새로 띄움: {str(e).splitlines()[0][:80]}')
      shared_driver = None

  options = ChromeOptions()
  options.add_experimental_option('detach', True)
  options.add_argument(f'--remote-debugging-port={_CHROME_DEBUG_PORT}')
  # 크롬 창을 내리거나 다른 창에 가리면, 크롬은 그 창을 '안 보이는 창'으로
  # 보고 화면 그리기를 멈춘다. 그러면 카드가 스르륵 나타나는 효과가 끝까지
  # 안 돌아서 글자가 계속 '투명(opacity 0)' 상태로 남고, 매크로는 문제를
  # 하나도 못 읽고 멈춘다. 아래 옵션들이 그걸 막아준다.
  for flag in ('--disable-backgrounding-occluded-windows',
               '--disable-renderer-backgrounding',
               '--disable-background-timer-throttling'):
    options.add_argument(flag)
  shared_driver = ChromeDriver(options=options)
  _keep_page_awake(shared_driver)
  shared_driver.get('https://www.classcard.net')
  return shared_driver


def _keep_page_awake(driver):
  """창을 내려놔도 크롬이 화면을 계속 그리도록 부탁한다(안 되면 그냥 넘어간다)."""
  try:
    driver.execute_cdp_cmd('Emulation.setFocusEmulationEnabled', {'enabled': True})
  except Exception:
    pass


# --- 클립보드에서 데이터 바로 읽기 ---
# --- 사이트에서 클래스/세트/단어 읽어오기 (완전 자동화용) ---
#
# 예전에는 북마크릿으로 단어를 복사해 와야 했는데, 로그인된 크롬이 있으면
# 세트 페이지에서 바로 읽을 수 있다. 클래스 목록과 세트 목록, 진도까지
# 같은 방식으로 읽어서 프로그램 안에서 고를 수 있게 한다.
_SITE = 'https://www.classcard.net'

_CLASS_LIST_JS = r"""
// 왼쪽 메뉴의 '나의 클래스' 목록.
//
// 링크 주소가 지금 보고 있는 화면에 따라 다르다. 클래스 화면에서는
// /ClassMain/<번호>인데 '내 정보' 화면에서는 /ClassReports/<번호>다.
// (이걸 몰라서 어떤 화면에서는 클래스를 하나도 못 읽었다.) 둘 다 본다.
var out = [], seen = {};
var skip = ['home', '세트', '설정', '리포트', '쪽지'];
document.querySelectorAll('a[href^="/ClassMain/"], a[href^="/ClassReports/"]').forEach(function (a) {
  var href = a.getAttribute('href') || '';
  var m = href.match(/^\/Class(?:Main|Reports)\/(?:users\/)?(\d+)$/);
  if (!m) return;
  var name = (a.textContent || '').replace(/\s+/g, ' ').trim();
  if (!name || skip.indexOf(name.toLowerCase()) !== -1) return;
  var key = m[1] + '|' + name;
  if (seen[key]) return;
  seen[key] = 1;
  out.push({idx: m[1], name: name});
});
return out;
"""

_SET_LIST_JS = r"""
// 클래스 화면의 세트 목록. 지금 진도인 세트와 종류(단어/문장)도 같이.
//
// 회색으로 흐리게 보이는 줄(class에 disabled)은 '잠긴 세트'가 아니다.
// 선생님이 지금 진도로 지정한 세트만 진하게 보이고 나머지는 흐리게 보일 뿐,
// 흐린 세트도 들어가면 카드가 다 있고 암기/리콜/스펠이 그대로 된다
// (2026-09-23 직접 확인). 그래서 막지 않고 current 표시만 한다.
var out = [];
document.querySelectorAll('.set-items[data-idx]').forEach(function (row) {
  var a = row.querySelector('.set-name-a');
  if (!a) return;
  var cnt = a.querySelector('span');
  var name = (a.textContent || '').replace(/\s+/g, ' ').trim();
  var count = cnt ? (cnt.textContent || '').trim() : '';
  if (count) name = name.replace(count, '').trim();
  var icon = row.querySelector('.set-icon');
  out.push({
    idx: row.getAttribute('data-idx'),
    name: name,
    count: count,
    current: row.className.indexOf('disabled') === -1,
    learning: row.className.indexOf('learn_set') !== -1,
    sentence: !!(icon && icon.className.indexOf('sentence') !== -1)
  });
});
return out;
"""

_SET_DETAIL_JS = r"""
// 세트 페이지: 카드(영어/뜻)와 진도(%), 구간 수를 읽는다.
function rate(sel) {
  var el = document.querySelector(sel);
  if (!el) return null;
  var m = (el.textContent || '').match(/(\d+)\s*%/);
  return m ? parseInt(m[1], 10) : null;
}
var cards = [];
document.querySelectorAll('.flip-card').forEach(function (c) {
  var eng = c.querySelector('.ex_front');
  var kor = c.querySelector('.ex_back');
  if (!eng || !kor) return;
  var e = (eng.innerText || '').trim();
  var k = (kor.innerText || '').trim();
  if (e && k) cards.push({eng: e, kor: k});
});
// 구간별 학습 버튼(10개 단위). 주소의 숫자가 구간 번호다.
// 그 구간을 그 모드로 다 끝냈으면 버튼에 progress-100이 붙는다.
var segs = [], segDone = {};
document.querySelectorAll('a[onclick*="/Memorize/"], a[onclick*="/Recall/"], a[onclick*="/Spell/"]').forEach(function (a) {
  var m = (a.getAttribute('onclick') || '').match(/\/(Memorize|Recall|Spell)\/(\d+)\/(\d+)\/(\d+)/);
  if (!m) return;
  var seg = parseInt(m[3], 10);
  if (seg >= 1000) return;  // 6000=전체, 4000=중요 카드만
  var mode = m[1].toLowerCase();
  if (segs.indexOf(seg) === -1) segs.push(seg);
  if (!segDone[seg]) segDone[seg] = {};
  segDone[seg][mode] = a.className.indexOf('progress-100') !== -1;
});
segs.sort(function (a, b) { return a - b; });
return {
  cards: cards,
  segments: segs,
  seg_done: segDone,
  memorize: rate('.mem-total-rate'),
  recall: rate('.recall-total-rate'),
  spell: rate('.spell-total-rate'),
  title: (document.title || '').replace('클래스카드 | ', '').trim()
};
"""


_RATES_BATCH_JS = r"""
// 세트 여러 개의 진도(%)를 한 번에 읽는다.
//
// 세트마다 페이지를 열어 이동하면 한 개에 1.5초쯤 걸려서, 세트가 80개인
// 클래스는 몇 분이 걸렸다. 대신 지금 열린 페이지에서 세트 페이지의 내용만
// 받아와(fetch) 화면에 안 붙이고 읽는다. 화면이 안 바뀌니 공부하던 탭을
// 건드리지도 않고, 다섯 개에 0.5초면 끝난다.
var cb = arguments[arguments.length - 1];
var ids = arguments[0], cls = arguments[1];
function one(id) {
  return fetch('/set/' + id + '/' + cls, {credentials: 'include'})
    .then(function (r) { return r.text(); })
    .then(function (t) {
      var doc = new DOMParser().parseFromString(t, 'text/html');
      function rate(sel) {
        var el = doc.querySelector(sel);
        if (!el) return null;
        var m = (el.textContent || '').match(/(\d+)\s*%/);
        return m ? parseInt(m[1], 10) : null;
      }
      var segs = [], segDone = {};
      doc.querySelectorAll('a[onclick*="/Memorize/"], a[onclick*="/Recall/"], a[onclick*="/Spell/"]').forEach(function (a) {
        var m = (a.getAttribute('onclick') || '').match(/\/(Memorize|Recall|Spell)\/(\d+)\/(\d+)\/(\d+)/);
        if (!m) return;
        var seg = parseInt(m[3], 10);
        if (seg >= 1000) return;
        var mode = m[1].toLowerCase();
        if (segs.indexOf(seg) === -1) segs.push(seg);
        if (!segDone[seg]) segDone[seg] = {};
        segDone[seg][mode] = a.className.indexOf('progress-100') !== -1;
      });
      segs.sort(function (a, b) { return a - b; });
      // 카드가 하나도 없으면 세트 페이지를 제대로 못 받은 것이다.
      var got = doc.querySelectorAll('.flip-card').length > 0;
      return {idx: String(id), ok: got, segments: segs, seg_done: segDone,
              memorize: rate('.mem-total-rate'),
              recall: rate('.recall-total-rate'),
              spell: rate('.spell-total-rate')};
    })
    .catch(function (e) { return {idx: String(id), ok: false}; });
}
Promise.all(ids.map(one)).then(cb);
"""


_CLASS_REPORT_JS = r"""
// 클래스의 '리포트' 화면 한 장에 세트별 진도와 테스트 결과가 다 들어 있다
// (/ClassReports/users/{클래스}). 세트마다 따로 열 필요 없이 한 번에 읽는다.
var cb = arguments[arguments.length - 1];
fetch('/ClassReports/users/' + arguments[0], {credentials: 'include'})
  .then(function (r) { return r.text(); })
  .then(function (t) {
    var doc = new DOMParser().parseFromString(t, 'text/html');
    var out = {};
    doc.querySelectorAll('div.class-report-panel').forEach(function (p) {
      // 세트 번호가 판 이름에 들어 있다: class="... penel-row10863783 ..."
      var m = (p.className || '').match(/penel-row(\d+)/);
      if (!m) return;
      var rec = {memorize: null, recall: null, spell: null, test: ''};
      p.querySelectorAll('.report-set > div').forEach(function (cell) {
        var s = (cell.textContent || '').replace(/\s+/g, ' ').trim();
        var pct = s.match(/(\d+)\s*%/);
        var n = pct ? parseInt(pct[1], 10) : null;
        if (s.indexOf('암기학습') === 0) rec.memorize = n;
        else if (s.indexOf('리콜학습') === 0) rec.recall = n;
        else if (s.indexOf('스펠학습') === 0) rec.spell = n;
      });
      // 테스트를 봤으면 결과 링크가 있다. 날짜와 점수가 <br>로 붙어 있어서
      // 그냥 읽으면 '6/8 16:10100점'처럼 엉킨다. 날짜를 떼서 '날짜 | 점수'로.
      var a = p.querySelector('a[href*="getTestReport"]');
      if (a) {
        var span = a.querySelector('span');
        var when = span ? (span.textContent || '').trim() : '';
        var rest = (a.textContent || '').replace(when, ' ')
                     .replace(/\s+/g, ' ').trim();
        rec.test = (when + ' | ' + rest).trim();
      }
      out[m[1]] = rec;
    });
    cb(out);
  })
  .catch(function (e) { cb(null); });
"""


def fetch_class_report(driver, class_idx):
  """클래스 리포트 한 장에서 세트별 진도 + 테스트 결과를 읽는다."""
  try:
    return driver.execute_async_script(_CLASS_REPORT_JS, str(class_idx)) or {}
  except Exception as e:
    print('[DEBUG] 리포트 읽기 실패:', str(e).splitlines()[0][:60])
    return {}


def fetch_set_rates(driver, class_idx, set_ids):
  """세트 여러 개의 진도를 한 번에 읽는다. {세트번호: {...}}"""
  try:
    out = driver.execute_async_script(
        _RATES_BATCH_JS, [str(i) for i in set_ids], str(class_idx))
  except Exception as e:
    print('[DEBUG] 진도 한꺼번에 읽기 실패:', str(e).splitlines()[0][:60])
    return {}
  return {x['idx']: x for x in (out or []) if x.get('ok')}


# 목록을 읽을 때 쓰는 탭. 공부 중인 탭을 빼앗지 않으려고 따로 둔다.
_data_tab = None


_STUDY_URL_PARTS = ('/memorize/', '/recall/', '/spell/', '/classtest/')


def _is_study_tab(url):
  url = (url or '').lower()
  return any(part in url for part in _STUDY_URL_PARTS)


def _enter_data_tab(driver):
  """목록을 읽을 탭으로 옮긴다. 원래 보던 탭 손잡이를 돌려준다.

  공부 중인 탭을 빼앗으면 안 되지만, 그렇다고 켤 때마다 새 탭을 만들면
  탭이 계속 쌓인다(사용자 불편). 그래서
    1) 지금 보고 있는 탭이 학습 화면이 아니면 그 탭을 그대로 쓰고,
    2) 학습 중이면 이미 있는 다른 탭 중 학습 화면이 아닌 것을 쓰고,
    3) 그런 탭도 없을 때만 새로 만든다."""
  global _data_tab
  try:
    prev = driver.current_window_handle
    handles = driver.window_handles
  except Exception:
    return None

  try:
    if not _is_study_tab(driver.current_url):
      _data_tab = prev
      return prev
  except Exception:
    pass

  if _data_tab in handles and _data_tab != prev:
    try:
      driver.switch_to.window(_data_tab)
      return prev
    except Exception:
      pass

  for handle in handles:
    if handle == prev:
      continue
    try:
      driver.switch_to.window(handle)
      if not _is_study_tab(driver.current_url):
        _data_tab = handle
        return prev
    except Exception:
      continue

  # 학습 중이 아니면 굳이 새 탭을 만들 필요가 없다. 지금 탭이 학습 주소여도
  # 아무도 안 쓰고 있으니 그대로 쓴다. (예전에는 [목록 새로고침]을 누를 때마다
  # 새 탭이 하나씩 생겼다.)
  try:
    driver.switch_to.window(prev)
  except Exception:
    return None
  if not (is_running or auto_running):
    _data_tab = prev
    return prev
  try:
    driver.switch_to.new_window('tab')
    _data_tab = driver.current_window_handle
  except Exception:
    return None
  return prev


def _leave_data_tab(driver, prev):
  try:
    if prev and prev in driver.window_handles:
      driver.switch_to.window(prev)
  except Exception:
    pass


def _wait_page_ready(driver, timeout=8.0):
  """페이지가 다 그려질 때까지 기다린다."""
  end = time.time() + timeout
  while time.time() < end:
    try:
      if driver.execute_script('return document.readyState;') == 'complete':
        return True
    except Exception:
      return False
    time.sleep(0.1)
  return False


def _open_site(driver, path):
  """크롬을 그 주소로 옮기고 화면이 그려질 때까지 기다린다."""
  url = path if path.startswith('http') else _SITE + path
  try:
    if (driver.current_url or '').rstrip('/') != url.rstrip('/'):
      driver.get(url)
    _wait_page_ready(driver)
    return True
  except Exception as e:
    print('[DEBUG] 주소 이동 실패:', str(e).splitlines()[0][:80])
    return False


def _read_until(driver, script, tries=6, gap=0.4):
  """목록이 채워질 때까지 몇 번 다시 읽는다.

  페이지를 연 직후에는 목록이 아직 안 그려져서 빈 목록이 나오는 일이
  있었다(클래스는 읽었는데 세트가 0개로 뜸)."""
  for i in range(tries):
    try:
      out = driver.execute_script(script) or []
    except Exception:
      out = []
    if out:
      return out
    if i < tries - 1:
      time.sleep(gap)
  return []


def fetch_classes(driver):
  """내 클래스 목록 [{'idx','name'}, ...]. 로그인 안 돼 있으면 빈 목록."""
  prev = _enter_data_tab(driver)
  try:
    if not _open_site(driver, '/Main/user'):
      return []
    # 로그인이 안 돼 있으면 목록은 영영 안 나온다. 그런데도 6번 다시 읽느라
    # 2초 넘게 서 있었다. 로그인부터 확인하고 바로 빈 목록을 돌려준다
    # (부르는 쪽이 곧장 자동 로그인으로 넘어간다).
    if not logged_in(driver):
      return []
    return _read_until(driver, _CLASS_LIST_JS)
  except Exception:
    return []
  finally:
    _leave_data_tab(driver, prev)


def fetch_sets(driver, class_idx):
  """클래스 안의 세트 목록."""
  prev = _enter_data_tab(driver)
  try:
    if not _open_site(driver, f'/ClassMain/{class_idx}'):
      return []
    return _read_until(driver, _SET_LIST_JS)
  except Exception:
    return []
  finally:
    _leave_data_tab(driver, prev)


def fetch_set_detail(driver, set_idx, class_idx):
  """세트의 카드(단어/문장), 구간 수, 모드별 진도(%)."""
  prev = _enter_data_tab(driver)
  try:
    if not _open_site(driver, f'/set/{set_idx}/{class_idx}'):
      return None
    data = {}
    for i in range(6):
      try:
        data = driver.execute_script(_SET_DETAIL_JS) or {}
      except Exception:
        data = {}
      if data.get('cards'):
        break
      if i < 5:
        time.sleep(0.4)
  except Exception:
    return None
  finally:
    _leave_data_tab(driver, prev)
  cards = data.get('cards') or []
  # 문장 세트는 같은 카드가 두 번씩 들어오는 일이 있어 중복을 뺀다.
  seen, uniq = set(), []
  for c in cards:
    key = (c.get('eng', ''), c.get('kor', ''))
    if key in seen:
      continue
    seen.add(key)
    uniq.append({'eng': c.get('eng', ''), 'kor': c.get('kor', '')})
  data['cards'] = uniq
  return data


# --- 자동 로그인 ---
#
# 비밀번호는 Windows가 주는 잠금(DPAPI)으로 암호화해서 이 컴퓨터에만 저장한다.
# 파일을 그대로 다른 컴퓨터로 옮겨도 풀리지 않고, 이 컴퓨터의 다른 사용자
# 계정에서도 못 푼다. 그래도 비밀번호를 저장하는 일이므로, 저장 여부는
# 사용자가 직접 고르게 한다(안 쓰면 예전처럼 직접 로그인).
_LOGIN_FILE = os.path.join(
    os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
    'ClasscardMacro', 'login.dat')


class _DataBlob(ctypes.Structure):
  _fields_ = [('cbData', ctypes.c_ulong),
              ('pbData', ctypes.POINTER(ctypes.c_char))]


def _dpapi(func, data):
  """Windows 잠금(DPAPI)으로 암호화/복호화."""
  buf = ctypes.create_string_buffer(data, len(data))
  blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
  blob_out = _DataBlob()
  ok = func(ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out))
  if not ok:
    raise OSError('Windows 암호화에 실패했습니다.')
  try:
    return ctypes.string_at(blob_out.pbData, blob_out.cbData)
  finally:
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def save_login(user_id, user_pw):
  """아이디/비밀번호를 암호화해서 저장한다."""
  raw = json.dumps({'id': user_id, 'pw': user_pw}).encode('utf-8')
  enc = _dpapi(ctypes.windll.crypt32.CryptProtectData, raw)
  os.makedirs(os.path.dirname(_LOGIN_FILE), exist_ok=True)
  with open(_LOGIN_FILE, 'wb') as f:
    f.write(enc)


def load_login():
  """저장해둔 아이디/비밀번호. 없으면 None."""
  try:
    with open(_LOGIN_FILE, 'rb') as f:
      enc = f.read()
    raw = _dpapi(ctypes.windll.crypt32.CryptUnprotectData, enc)
    data = json.loads(raw.decode('utf-8'))
    if data.get('id') and data.get('pw'):
      return data
  except Exception:
    return None
  return None


def clear_login():
  try:
    os.remove(_LOGIN_FILE)
    return True
  except Exception:
    return False


def _dismiss_alert(driver):
  """사이트가 띄운 경고창을 닫는다. 닫았으면 True.

  경고창이 떠 있으면 매크로가 크롬을 조작할 수 없으므로 꼭 닫아야 한다."""
  try:
    alert = driver.switch_to.alert
  except Exception:
    return False
  try:
    text = (alert.text or '').strip()
    alert.accept()
    print(f'[AUTO] 사이트 경고창 닫음: {text[:60]}')
  except Exception:
    return False
  return True


_LOGIN_FILL_JS = r"""
// 아이디/비밀번호 칸을 찾아서 한 번에 채운다. 채운 글자 수를 돌려준다.
function visible(el) {
  if (!el) return null;
  var r = el.getBoundingClientRect();
  return (r.width > 0 && r.height > 0) ? el : null;
}
var id = visible(document.querySelector('input[name="login_id"]'))
      || visible(document.querySelector('#login_id'))
      || visible(document.querySelector('input[name="user_id"]'));
var pw = visible(document.querySelector('input[type="password"]'))
      || visible(document.querySelector('input[name="login_pwd"]'))
      || visible(document.querySelector('#login_pwd'));
if (!id || !pw) return 0;
function fill(el, value) {
  el.focus();
  el.value = value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
}
fill(id, arguments[0]);
fill(pw, arguments[1]);
return (pw.value || '').length;
"""


def logged_in(driver):
  """지금 크롬이 로그인된 상태인지."""
  try:
    return bool(driver.execute_script(
        "return !!(window.c_u && window.c_u > 0);"))
  except Exception:
    return False


def auto_login(driver):
  """저장해둔 계정으로 로그인한다. 성공하면 True."""
  cred = load_login()
  if not cred:
    return False
  print('[AUTO] 저장된 계정으로 로그인 시도')
  _dismiss_alert(driver)
  if not _open_site(driver, '/Login'):
    return False
  _dismiss_alert(driver)
  if logged_in(driver):
    return True

  # 로그인 화면을 열자마자 사이트가 경고창을 띄우는 일이 있다(로그: 'error').
  # 그 창을 닫으면 화면이 다시 그려져서, 미리 잡아둔 칸 손잡이가 끊어진다
  # (stale element). 그래서 칸을 찾는 일과 글자를 넣는 일을 자바스크립트로
  # 한 번에 처리하고, 버튼만 셀레니움으로 누른다.
  #
  # 기다리는 시간은 전부 '될 때까지 짧게 여러 번' 방식이다. 예전에는 칸이
  # 그려지길 0.6초 통째로 자고, 로그인됐는지도 0.5초마다만 봐서 느렸다.
  for attempt in range(3):
    if attempt:
      _open_site(driver, '/Login')
    _dismiss_alert(driver)

    # 아이디/비밀번호 칸이 그려지는 즉시 채운다(최대 3초).
    typed = 0
    end = time.time() + 3.0
    while time.time() < end:
      try:
        typed = driver.execute_script(_LOGIN_FILL_JS, cred['id'], cred['pw'])
      except Exception:
        typed = 0
        if _dismiss_alert(driver):
          continue
      if typed:
        break
      time.sleep(0.1)
    if not typed:
      print('[AUTO] 로그인 칸을 못 찾음')
      continue

    if not click_button_by_text(driver, ['로그인'], wait_after=0):
      try:
        driver.execute_script(
            "var f = document.querySelector('input[name=\"login_pwd\"]');"
            "if (f && f.form) f.form.submit();")
      except Exception:
        pass

    # 로그인되는 즉시 넘어간다(최대 8초).
    end = time.time() + 8.0
    while time.time() < end:
      if logged_in(driver):
        print('[AUTO] 로그인 완료')
        return True
      if _dismiss_alert(driver):
        break
      time.sleep(0.12)
  print('[AUTO] 로그인 실패 (아이디/비밀번호 확인)')
  return False


def open_login_settings():
  """자동 로그인에 쓸 계정을 넣는 작은 창."""
  win = tk.Toplevel(root)
  win.title('자동 로그인')
  win.resizable(False, False)
  win.wm_attributes('-topmost', True)

  tk.Label(win, text='클래스카드 계정', font=('맑은 고딕', 12, 'bold')).pack(
      padx=24, pady=(18, 4))
  tk.Label(win,
           text='저장해두면 로그인이 풀렸을 때 프로그램이 알아서 로그인합니다.\n'
                '비밀번호는 이 컴퓨터(Windows 계정)에서만 풀 수 있게 암호화해\n'
                '저장합니다. 저장하지 않으면 예전처럼 크롬에서 직접 로그인하면 됩니다.',
           font=('맑은 고딕', 9), fg='gray', justify='center').pack(padx=24)

  frame = tk.Frame(win)
  frame.pack(padx=24, pady=12)
  tk.Label(frame, text='아이디', font=('맑은 고딕', 10)).grid(row=0, column=0,
                                                          sticky='e', pady=4)
  ent_id = tk.Entry(frame, width=24, font=('맑은 고딕', 10))
  ent_id.grid(row=0, column=1, padx=8, pady=4)
  tk.Label(frame, text='비밀번호', font=('맑은 고딕', 10)).grid(row=1, column=0,
                                                           sticky='e', pady=4)
  ent_pw = tk.Entry(frame, width=24, show='*', font=('맑은 고딕', 10))
  ent_pw.grid(row=1, column=1, padx=8, pady=4)

  saved = load_login()
  if saved:
    ent_id.insert(0, saved['id'])
    ent_pw.insert(0, saved['pw'])

  lbl = tk.Label(win, text='저장된 계정이 있습니다.' if saved else '저장된 계정이 없습니다.',
                 font=('맑은 고딕', 9), fg='gray')
  lbl.pack()

  def do_save():
    user_id = ent_id.get().strip()
    user_pw = ent_pw.get()
    if not user_id or not user_pw:
      messagebox.showwarning('자동 로그인', '아이디와 비밀번호를 모두 적어주세요.')
      return
    try:
      save_login(user_id, user_pw)
    except Exception as e:
      messagebox.showerror('자동 로그인', f'저장하지 못했습니다:\n{e}')
      return
    lbl.config(text='저장했습니다.', fg='#2E7D32')

  def do_clear():
    clear_login()
    ent_id.delete(0, tk.END)
    ent_pw.delete(0, tk.END)
    lbl.config(text='저장된 계정을 지웠습니다.', fg='#D32F2F')

  btns = tk.Frame(win)
  btns.pack(pady=(8, 18))
  tk.Button(btns, text='저장', width=10, font=('맑은 고딕', 10, 'bold'),
            fg='#2E7D32', command=do_save).pack(side='left', padx=6)
  tk.Button(btns, text='지우기', width=10, font=('맑은 고딕', 10),
            fg='#C62828', command=do_clear).pack(side='left', padx=6)
  tk.Button(btns, text='닫기', width=10, font=('맑은 고딕', 10),
            command=win.destroy).pack(side='left', padx=6)


def _set_loaded_words(cards, source, set_idx=None):
  """불러온 카드로 word_list를 채우고 버튼을 켠다."""
  global word_list, loaded_set_idx
  loaded_set_idx = str(set_idx) if set_idx else None
  seen = set()
  word_list = []
  for item in cards:
    key = (item.get('eng', ''), item.get('kor', ''))
    if not key[0] or not key[1] or key in seen:
      continue
    seen.add(key)
    word_list.append({'eng': item['eng'], 'kor': item['kor']})
  lbl_status.config(
      text=f'준비 완료! {source}에서 {len(word_list)}개 불러옴\n'
           '모드에 맞는 버튼을 누르세요.',
      fg='#388E3C',
  )
  btn_start.config(state=tk.NORMAL)
  btn_recall_start.config(state=tk.NORMAL)
  btn_spell_start.config(state=tk.NORMAL)
  btn_test_start.config(state=tk.NORMAL)
  on_kind_change()  # 문장 모드에서 막아둔 버튼은 계속 막아둔다


def _idx_from_url(url):
  """지금 열려 있는 주소에서 (세트 번호, 클래스 번호)를 뽑는다."""
  url = (url or '').split('?')[0]
  m = re.search(r'/(?:Memorize|Recall|Spell)/(\d+)/\d+/(\d+)', url, re.I)
  if m:
    return m.group(1), m.group(2)
  m = re.search(r'/set/(\d+)/(\d+)', url, re.I)
  if m:
    return m.group(1), m.group(2)
  # 테스트 주소만 세트와 클래스 순서가 반대다(/ClassTest/클래스/세트).
  m = re.search(r'/ClassTest/(\d+)/(\d+)', url, re.I)
  if m:
    return m.group(2), m.group(1)
  return None, None


_SET_CARDS_FETCH_JS = r"""
// 세트 페이지의 카드만 몰래 읽어온다. 탭을 옮기지도, 주소를 바꾸지도
// 않는다(같은 classcard.net 안에 있을 때만 쓸 수 있다).
var cb = arguments[arguments.length - 1];
fetch('/set/' + arguments[0] + '/' + arguments[1], {credentials: 'include'})
  .then(function (r) { return r.text(); })
  .then(function (t) {
    var doc = new DOMParser().parseFromString(t, 'text/html');
    var out = [];
    doc.querySelectorAll('.flip-card').forEach(function (c) {
      var e = c.querySelector('.ex_front'), k = c.querySelector('.ex_back');
      if (!e || !k) return;
      var a = (e.textContent || '').trim(), b = (k.textContent || '').trim();
      if (a && b) out.push({eng: a, kor: b});
    });
    cb(out);
  })
  .catch(function () { cb(null); });
"""


def fetch_set_cards(driver, set_idx, class_idx):
  """세트의 카드를 '공부하던 화면 그대로' 읽는다(이동/새 탭 없음)."""
  try:
    cards = driver.execute_async_script(
        _SET_CARDS_FETCH_JS, str(set_idx), str(class_idx))
  except Exception as e:
    print('[DEBUG] 세트 카드 읽기 실패:', str(e).splitlines()[0][:60])
    return []
  seen, uniq = set(), []
  for c in cards or []:
    key = ((c.get('eng') or '').strip(), (c.get('kor') or '').strip())
    if not key[0] or not key[1] or key in seen:
      continue
    seen.add(key)
    uniq.append({'eng': key[0], 'kor': key[1]})
  return uniq


# 지금 word_list에 담긴 게 어느 세트 것인지. 화면과 어긋났는지 볼 때 쓴다.
loaded_set_idx = None
_last_list_sync = 0.0


def study_card_total(driver):
  """이 학습 화면이 '몇 장짜리'인지. 화면 위쪽의 '6/12' 표시에서 뒤 숫자를
  읽는다. 못 읽으면 -1."""
  try:
    out = driver.execute_script(_PROGRESS_JS)
  except Exception:
    return -1
  if out and len(out) == 2:
    return int(out[1])
  return -1


def warn_if_no_cards(driver):
  """할 카드가 0장이면 화면에 알려주고 True. 아니면 False.

  이미 100%까지 끝낸 구간에 다시 들어가면 사이트가 '0/0'을 주면서 아무
  문제도 안 낸다. 예전에는 매크로가 그걸 모르고 [영작 연습하기]만 끝없이
  누르고 있어서, 사용자 눈에는 '들어왔는데 인식을 못 한다'로 보였다."""
  if study_card_total(driver) != 0:
    return False
  print('[DEBUG] 이 학습 화면에 할 카드가 0장이다(0/0)')
  root.after(0, lambda: lbl_status.config(
      text='이 학습에 남은 카드가 없습니다(0/0).\n'
           '이미 100%까지 끝낸 구간이면 세트 화면에서 다음 구간이나 '
           '다른 세트를 골라주세요.', fg='#D32F2F'))
  return True


def ensure_word_list_for_screen(driver, min_gap=3.0):
  """화면에 열린 세트와 불러온 단어가 다르면, 화면 쪽 세트로 다시 읽어온다.

  수동 모드에서 제일 흔한 사고다. A 세트를 불러온 채로 B 세트의 학습
  화면에 들어가면, 화면은 멀쩡한데 아는 문장/단어가 하나도 없어서 매크로가
  '학습 화면을 기다리는 중'에서 가만히 있는다 - 사용자 눈에는 '들어왔는데
  인식을 못 한다'로 보인다. 이제는 알아서 화면 쪽 세트로 맞춘다.

  자동 학습은 스스로 세트를 맞춰 넣으므로 건드리지 않는다."""
  global word_list, loaded_set_idx, _last_list_sync
  if auto_running:
    return False
  now = time.time()
  if now - _last_list_sync < min_gap:
    return False
  _last_list_sync = now
  try:
    url = driver.current_url or ''
  except Exception:
    return False
  set_idx, class_idx = _idx_from_url(url)
  if not set_idx or not class_idx:
    return False
  if word_list and str(loaded_set_idx) == str(set_idx):
    return False
  cards = fetch_set_cards(driver, set_idx, class_idx)
  if not cards:
    return False
  word_list = cards
  loaded_set_idx = str(set_idx)
  print(f'[DEBUG] 화면에 열린 세트({set_idx})로 {len(cards)}개 다시 읽음')
  root.after(0, lambda n=len(cards): lbl_status.config(
      text=f'화면에 열린 세트에서 {n}개를 불러왔습니다. 이어서 풉니다...',
      fg='#0288D1'))
  return True


def load_words():
  """크롬에 열려 있는 세트에서 단어/문장을 바로 읽어온다.

  예전에는 북마크릿으로 단어를 복사해 클립보드로 넘겨야 했다. 로그인된
  크롬이 있으면 세트 페이지에서 그대로 읽을 수 있어서 그 과정이 필요 없다.
  주소로 세트를 못 알아내면 예전처럼 클립보드에서 읽는다."""

  def work():
    try:
      driver = get_driver()
    except Exception as e:
      err = str(e)
      root.after(0, lambda: messagebox.showerror(
          '불러오기', f'크롬을 열지 못했습니다:\n{err}'))
      return
    try:
      url = driver.current_url
    except Exception:
      url = ''
    set_idx, class_idx = _idx_from_url(url)
    if not set_idx:
      root.after(0, lambda: lbl_status.config(
          text='크롬에서 공부할 단어장을 먼저 열어주세요. (클립보드로 시도합니다)',
          fg='#D32F2F'))
      root.after(0, load_from_clipboard)
      return
    root.after(0, lambda: lbl_status.config(
        text='세트에서 단어를 읽는 중...', fg='#0288D1'))
    detail = fetch_set_detail(driver, set_idx, class_idx)
    if not detail or not detail.get('cards'):
      root.after(0, lambda: lbl_status.config(
          text='세트에서 단어를 못 읽었습니다. 클립보드로 시도합니다.', fg='#D32F2F'))
      root.after(0, load_from_clipboard)
      return
    cards = detail['cards']
    # 단어장 종류도 자동으로 맞춘다(문장 세트면 문장 모드로).
    kind = None
    sets = fetch_sets(driver, class_idx) if class_idx else []
    for item in sets:
      if str(item.get('idx')) == str(set_idx):
        kind = 'sentence' if item.get('sentence') else 'word'
        break
    if kind is None:
      long_cards = sum(1 for c in cards if ' ' in (c.get('eng') or '').strip())
      kind = 'sentence' if long_cards > len(cards) / 2 else 'word'

    def done():
      if SENTENCE_MODE_ENABLED:
        study_kind.set(kind)
      _set_loaded_words(cards, detail.get('title') or '세트', set_idx)

    root.after(0, done)

  threading.Thread(target=work, daemon=True).start()


def load_from_clipboard():
  global word_list
  word_list.clear()

  try:
    raw_text = root.clipboard_get().strip()
    data = json.loads(raw_text)

    if isinstance(data, list) and len(data) > 0 and 'eng' in data[0]:
      # 문장 단어장은 북마크릿이 카드를 두 번씩 긁어온다(24문장 -> 48개).
      # 중복이 있으면 같은 문장을 두 번 처리하려 하므로 순서를 지키면서
      # 걸러낸다.
      seen = set()
      word_list = []
      for item in data:
        key = (item.get('eng', ''), item.get('kor', ''))
        if key in seen:
          continue
        seen.add(key)
        word_list.append(item)
      _set_loaded_words(word_list, '클립보드')
    else:
      messagebox.showerror('에러', '올바른 클래스카드 데이터가 아닙니다.')
  except Exception:
    messagebox.showerror(
        '에러',
        '클립보드에 데이터가 없습니다.\n먼저 브라우저 북마크를 눌러주세요.',
    )


# --- 암기 학습 자동 진행 스레드 ---
def memo_worker():
  """단어 암기: 카드에서 space -> shift+space -> 오른쪽 화살표를 반복한다.

  예전에는 PyAutoGUI로 화면 좌표를 클릭해 포커스를 넘긴 뒤 OS 레벨로 키를
  보냈다(시작 전 5초 동안 마우스를 카드 위로 옮겨야 했다). 이제는 다른
  모드와 똑같이 공유 크롬 창에 셀레니움으로 키를 보내므로, 마우스를 어디
  두든 상관없고 창이 따로 뜨지도 않는다.

  주소가 /Memorize/로 바뀌어도 사이트에서 [시작]을 눌러야 학습이 시작되기
  때문에, 화면을 감지하면 5초를 세고 나서 키를 보내기 시작한다."""
  global is_running

  is_running = True
  btn_load.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_spell_start.config(state=tk.DISABLED)
  btn_test_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  try:
    driver = get_driver()
    root.after(
        0,
        lambda: lbl_status.config(
            text=('크롬에서 로그인 후 암기 학습 화면으로 이동하세요.\n'
                  '화면이 뜨면 자동으로 시작합니다.'),
            fg='#0288D1',
        ),
    )

    ready = False
    waiting_go = False
    # 자동 학습 중에는 사람이 [시작]을 누를 필요가 없다.
    # (예전에는 여기서 무조건 memo_go를 지웠다. 그런데 자동 학습은 이 스레드를
    #  띄우기 직전에 memo_go를 켜두기 때문에, 켜둔 신호가 곧바로 지워져서
    #  암기가 '[시작] 버튼 대기'에서 영영 멈춰 있었다.)
    if auto_running:
      memo_go.set()
    else:
      memo_go.clear()

    while is_running:
      # 구간이 끝나 완료 화면이 떠 있으면 먼저 다음 구간으로 넘긴다.
      if handle_section_done(driver):
        continue

      # 암기 학습 화면에 들어왔을 때만 키를 보낸다. 로그인하고 학습
      # 화면까지 들어가는 동안은 조용히 기다린다.
      if not on_memorize_screen(driver):
        # 다른 탭에 암기 화면이 열려 있으면 그 탭으로 옮긴다.
        if focus_study_tab(driver, _MEMORIZE_URL_HINT) and on_memorize_screen(
            driver
        ):
          continue
        if ready or waiting_go:
          print('[DEBUG] 암기 화면을 벗어남 - 다시 대기')
          ready = False
          waiting_go = False
          memo_go.clear()
          root.after(0, lambda: btn_memo_go.config(state=tk.DISABLED))
        root.after(
            0,
            lambda: lbl_status.config(
                text='암기 학습 화면을 기다리는 중...', fg='gray'
            ),
        )
        time.sleep(0.5)
        continue

      if not ready:
        # 주소가 바뀌어도 사이트에서 [시작]을 눌러야 학습이 시작된다. 그
        # 사이에 키를 보내면 엉뚱한 곳에 들어가므로, 사용자가 준비됐다고
        # 매크로의 [시작] 버튼을 누를 때까지 기다린다. (예전엔 5초를 셌는데
        # 타이밍이 안 맞으면 불편해서 버튼으로 바꿨다)
        if not memo_go.is_set():
          if not waiting_go:
            print('[DEBUG] 암기 화면 감지 - [시작] 버튼 대기')
            waiting_go = True
            root.after(0, lambda: btn_memo_go.config(state=tk.NORMAL))
            root.after(
                0,
                lambda: lbl_status.config(
                    text='사이트에서 학습을 시작한 뒤 [시작]을 누르세요.',
                    fg='#1976D2',
                ),
            )
          time.sleep(0.1)
          continue

        waiting_go = False
        ready = True
        root.after(
            0,
            lambda: lbl_status.config(
                text='암기 자동 학습 진행 중... ([정지] 클릭 시 중단)',
                fg='#388E3C',
            ),
        )

      try:
        driver.find_element(By.TAG_NAME, 'body').click()
      except Exception:
        pass
      time.sleep(0.1)

      ActionChains(driver).send_keys(Keys.SPACE).perform()
      dispatch_key(driver, ' ')
      time.sleep(0.2)
      if not is_running:
        break

      ActionChains(driver).key_down(Keys.SHIFT).send_keys(
          Keys.SPACE
      ).key_up(Keys.SHIFT).perform()
      time.sleep(0.2)
      if not is_running:
        break

      ActionChains(driver).send_keys(Keys.ARROW_RIGHT).perform()
      _auto_card_done()  # 카드 한 장 넘겼다(자동 학습이 한 바퀴를 세는 데 쓴다)
      time.sleep(0.3)

  except Exception as e:
    err_msg = str(e)
    root.after(
        0,
        lambda: messagebox.showerror(
            '셀레니움 에러', f'오류가 발생했습니다:\n{err_msg}'
        ),
    )
  finally:
    root.after(0, stop_macro)


def memo_go_pressed():
  """[시작] 버튼: 암기 화면에서 기다리던 매크로를 출발시킨다."""
  memo_go.set()
  btn_memo_go.config(state=tk.DISABLED)


def start_macro():
  if not word_list:
    messagebox.showwarning('알림', '먼저 단어를 불러와주세요.')
    return

  t = threading.Thread(target=memo_worker, daemon=True)
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


# --- 문장 낱말 배열 자동 풀이 스레드 ---
# 문장 단어장은 암기(영작 연습)도, 리콜(듣고 배열하기)도 낱말 조각을 순서대로
# 누르는 방식이라 같은 워커를 쓴다. 리콜은 문제가 소리로만 나오지만, 조각
# 구성만 보면 어떤 문장인지 알 수 있어서 들을 필요가 없다.
def run_sentence_scramble(mode_name='문장'):
  """문장 모드 공통 실행. mode_name은 상태 표시에만 쓴다(예: '리콜')."""
  if not word_list:
    messagebox.showwarning('알림', '먼저 [불러오기]로 문장을 로드해주세요.')
    return

  t = threading.Thread(
      target=sentence_scramble_worker, args=(mode_name,), daemon=True)
  t.start()


def _strip_pos_tag(text):
  """'[명] 북극곰' 같은 한글 뜻 문자열에서 앞의 품사 태그를 떼고
  '북극곰'만 남긴다. 테스트 모드 보기 박스는 태그 없이 뜻만 표시하기
  때문에 비교 시 이 형태와도 맞춰봐야 한다."""
  return re.sub(r'^\[[^\]]+\]\s*', '', text).strip()


def _norm_space(text):
  """앞뒤 공백을 없애고 중간 공백도 한 칸으로 통일한다."""
  return re.sub(r'\s+', ' ', text or '').strip()


# 클래스카드 문장에는 일반 따옴표 대신 둥근 따옴표가 쓰인다(I’m). 화면
# 텍스트와 비교할 때 서로 다른 글자로 취급되면 매칭이 통째로 실패한다.
_QUOTE_MAP = {
    '‘': "'",
    '’': "'",
    '“': '"',
    '”': '"',
}


def _norm_quotes(text):
  out = _norm_space(text)
  for src, dst in _QUOTE_MAP.items():
    out = out.replace(src, dst)
  return out


def _norm_token(text):
  """문장 조각 비교용으로 낱말 하나를 정규화한다.

  화면의 조각에는 문장부호가 빠져 있어서('Hi,' -> 'Hi') 양쪽 모두
  부호를 떼고 소문자로 맞춘다. 아포스트로피는 낱말 안에 들어가므로
  (I'm) 뗀 뒤 비교해도 되게 함께 제거한다."""
  t = _norm_quotes(text).lower()
  cleaned = re.sub(r"[^0-9a-zÀ-ɏ]+", '', t)
  # 'a pot & small stones'처럼 기호 하나가 낱말로 나오는 문장이 있다.
  # 기호를 다 지우면 그 낱말이 사라져서, 화면의 '&' 조각을 건너뛰고 다음
  # 낱말을 눌러 오답이 났다. 글자가 하나도 안 남으면 기호 그대로 쓴다.
  return cleaned or t.strip()


def _sentence_tokens(sentence):
  """영어 문장을 화면 조각과 같은 형태의 낱말 목록으로 쪼갠다."""
  return [t for t in (_norm_token(w) for w in _norm_quotes(sentence).split()) if t]


def _sentence_chunks(sentence):
  """맞춰볼 후보 목록: 문장 전체 + ' / '로 나뉜 토막들.

  '끊어읽기' 세트는 카드 하나가
  'Peter is visiting Korea / to meet a friend, Mina, / from a sister school.'
  처럼 토막 나 있고, 사이트는 보통 토막을 하나씩 따로 물어본다. 짧은 카드는
  토막을 한꺼번에 묻기도 해서 '전체'도 후보에 넣는다.

  이때 끊는 표시 '/'는 낱말이 아니므로 빼야 한다. 안 빼면 문장 낱말 수가
  하나 더 많아져서, 조각이 다 있는데도 '어느 구간과도 안 맞는다'며 멈췄다.
  (`and/or`처럼 붙어 있는 빗금은 낱말의 일부이므로 양쪽이 띄어져 있을 때만
   끊는 표시로 본다)"""
  parts = re.split(r'\s+/\s+', sentence)
  if len(parts) == 1:
    return [sentence]
  return [' '.join(parts)] + [part for part in parts if part.strip()]


def _norm_meaning(text):
  """뜻 비교용 정규화.

  같은 뜻인데 구분 기호만 다른 경우가 있다 - 단어장에는
  '낭비하다; 쓰레기, 낭비'로 들어 있는데 화면 보기는
  '낭비하다, 쓰레기, 낭비'로 나온다. 세미콜론을 쉼표로 맞추고, 기호 둘레의
  공백과 끝에 붙은 마침표/기호를 정리한다."""
  t = _norm_quotes(text).replace(';', ',').replace('·', ',')
  t = re.sub(r'\s*,\s*', ',', t)
  return t.strip().strip(',.')


def _pick_choice(choices, target):
  """보기 목록에서 정답 보기를 골라 (인덱스, 요소, 텍스트)를 돌려준다.

  단순 포함(in) 비교만 하면 '[형] 신'을 찾을 때 위에 있는 '[형] 신축성
  있는'이 먼저 걸려서 오답을 골랐다(리콜/테스트 양쪽에서 실제로 발생).
  완전히 같은 것 -> 품사 태그만 뗀 것 -> 구분 기호까지 맞춘 것 -> 마지막
  수단으로 포함 관계 순으로 단계를 나눠서, 정확한 보기가 있으면 항상
  그쪽을 고른다."""
  target_norm = _norm_space(target)
  target_stripped = _strip_pos_tag(target_norm)
  target_meaning = _norm_meaning(target_stripped)

  items = []
  for idx, (el, raw) in enumerate(choices):
    txt = _norm_space(raw)
    if txt:
      stripped = _strip_pos_tag(txt)
      items.append((idx, el, txt, stripped, _norm_meaning(stripped)))

  for idx, el, txt, _, _m in items:
    if txt == target_norm:
      return idx, el, txt
  for idx, el, txt, stripped, _m in items:
    if target_stripped and stripped == target_stripped:
      return idx, el, txt
  for idx, el, txt, _s, meaning in items:
    if target_meaning and meaning == target_meaning:
      return idx, el, txt
  for idx, el, txt, _s, _m in items:
    if target_norm in txt or txt in target_norm:
      return idx, el, txt
  return None


_VISIBLE_JS = """
function isReallyVisible(el) {
  // 크롬 창을 내리거나 다른 탭을 보고 있으면(document.hidden) 이 페이지는
  // '안 보이는 문서'가 되어, checkVisibility가 화면에 멀쩡히 그려진 것까지
  // 전부 false로 돌려준다. 그러면 매크로가 문제를 하나도 못 읽고 멈춘다
  // (창을 내려놓으면 학습이 멈추던 원인). 그럴 때는 아래 직접 검사로 판단한다.
  if (!document.hidden && typeof el.checkVisibility === 'function') {
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


# 이 사이트는 학습 종류가 주소에 그대로 드러난다.
# 예) classcard.net/Memorize/21175530/1/1651515
_MEMORIZE_URL_HINT = '/memorize/'


def on_memorize_screen(driver):
  """암기 학습 화면인지 URL로 판별한다.

  처음엔 카드에 아는 단어가 떠 있는지로 판별하려 했는데, 암기 화면 카드는
  .flip-card가 아니어서 하나도 못 찾고 계속 대기만 했다. 주소가 가장
  확실하다."""
  try:
    return _MEMORIZE_URL_HINT in (driver.current_url or '').lower()
  except Exception:
    return False


_BUTTON_BY_TEXT_JS = r"""
// 학습이 멈춰 서는 화면들(구간 완료 "GOOD JOB!!", 세트 완료 "100% Clear!!"
// 등)에는 문제도 보기도 없어서 매크로가 그대로 멈춰 있었다. 화면에 보이는
// 진행 버튼을 찾아서 돌려준다. 텍스트가 정확히 일치하거나 patterns 중
// 하나와 맞는 것만 보므로, 같은 문구를 품고 있는 바깥 div는 안 걸린다.
var wanted = arguments[0] || [];
var patterns = arguments[1] || [];
var nodes = document.querySelectorAll('a, button, input[type="button"], div, span, p');
for (var i = 0; i < nodes.length; i++) {
  var el = nodes[i];
  var t = (el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
  if (!t) continue;
  var hit = wanted.indexOf(t) !== -1;
  for (var k = 0; !hit && k < patterns.length; k++) {
    try {
      if (new RegExp(patterns[k]).test(t)) hit = true;
    } catch (e) {}
  }
  if (!hit) continue;
  // 창을 내려놓으면(document.hidden) checkVisibility가 전부 false라 버튼을
  // 하나도 못 찾는다. 그럴 때는 크기로만 판단한다.
  if (!document.hidden && typeof el.checkVisibility === 'function') {
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
# 위 세 개 중 [다음 구간으로 이동]만 '같은 바퀴를 이어서'다. [계속하기]는
# 세트를 다 끝낸 뒤 '한 바퀴 더'라서, 자동 학습에서는 누르면 안 된다.
# (스펠이 여기서 계속 눌려 같은 세트를 340%까지 돌고 있었다)
_SECTION_NEXT_LABELS = ['다음 구간으로 이동', '다음 구간 이동']
# 테스트를 다 풀면 '와우 100점이에요!' 화면에 [제출 결과 확인]만 남는다.
# 이것도 '다 끝났다'는 신호다(이걸 모르고 30분이나 기다린 적이 있다).
_SECTION_AGAIN_LABELS = ['계속하기', '제출 결과 확인']
# 테스트를 다 본 화면의 버튼. 단어 테스트는 [제출 결과 확인], 문장 테스트는
# 점수와 [완료]만 뜬다. '완료'는 흔한 낱말이라 테스트 화면에서만 본다.
_TEST_DONE_LABELS = ['완료', '제출 결과 확인', '결과 확인']
# 테스트에서 덜 배열한 채 제출하면 '아직 배열하지 않은 단어가 있습니다'
# 확인창이 뜬다. 떠 있으면 아무것도 못 누르므로 [취소]로 닫는다.
_CANCEL_LABELS = ['취소']
# 단어장에 따라 답을 고른 뒤 자동으로 안 넘어가고 [다음 카드](SPACE)를
# 기다리는 화면이 있다.
_NEXT_CARD_LABELS = ['다음 카드', '다음카드', '다음 문제']
# 세트를 다 끝내면 "100% Clear!!" 화면에 [200% 도전]이 뜬다. 퍼센트는
# 계속 올라가므로 숫자를 고정하지 않고 패턴으로 잡는다.
_SECTION_DONE_PATTERNS = [r'^\d+% ?도전$']
_WRITE_PRACTICE_LABELS = ['영작 연습하기']


_CLASS_TEST_URL_HINT = '/classtest/'


_STUDY_URL_HINTS = ('/memorize/', '/recall/', '/spell/', '/classtest/')


def focus_study_tab(driver, hint=None):
  """학습 화면이 열려 있는 탭으로 옮긴다.

  셀레니움은 처음 잡은 탭만 들여다본다. 사용자가 새 탭에서 학습 화면을 열면
  매크로는 엉뚱한 탭을 보며 '화면을 기다리는 중'만 반복한다(화면에는 문제가
  멀쩡히 떠 있는데 아무것도 못 읽는 증상).

  hint를 주면 그 학습 종류의 탭만 찾는다. 탭을 여러 개 열어두면 지난
  학습 탭이 먼저 걸려서 엉뚱한 화면을 붙들고 있을 수 있다.

  탭 주소는 탭을 전환하지 않고 읽는다(Target.getTargets). 처음엔 탭을 하나씩
  switch_to.window로 넘겨 가며 확인했는데, 그 호출이 최소화한 크롬 창을 매번
  되살려서 창을 내려둘 수가 없었다(실험으로 확인: 클릭/키 입력/스크립트는
  최소화를 유지하고 switch_to.window만 창을 복원한다). 이제는 정말 다른
  탭으로 옮겨야 할 때 딱 한 번만 전환한다."""
  wanted = (hint,) if hint else _STUDY_URL_HINTS

  def matches(url):
    low = (url or '').lower()
    return any(h in low for h in wanted)

  try:
    if matches(driver.current_url):
      return True
  except Exception:
    return False

  try:
    targets = driver.execute_cdp_cmd('Target.getTargets', {})
  except Exception:
    return False

  try:
    current = driver.current_window_handle
  except Exception:
    current = None

  for info in targets.get('targetInfos', []):
    if info.get('type') != 'page' or not matches(info.get('url')):
      continue
    handle = info.get('targetId')
    if not handle or handle == current:
      continue
    try:
      # 셀레니움 창 핸들은 이 targetId와 같다.
      driver.switch_to.window(handle)
      print(f"[DEBUG] 학습 화면이 열린 탭으로 이동: {info.get('url')}")
      return True
    except Exception:
      continue
  return False


def on_class_test(driver):
  """문장 테스트(어순배열) 화면인지 주소로 판별한다."""
  try:
    return _CLASS_TEST_URL_HINT in (driver.current_url or '').lower()
  except Exception:
    return False


def press_submit(driver):
  """배열을 마친 뒤 제출한다.

  암기/리콜/스펠은 스페이스로 넘어가지만, 테스트(어순배열)는 [제출] 버튼에
  ENTER가 붙어 있다. 주소로 구분한다."""
  if not on_class_test(driver):
    return press_space(driver)

  try:
    driver.find_element(By.TAG_NAME, 'body').click()
    time.sleep(0.1)
    ActionChains(driver).send_keys(Keys.RETURN).perform()
    return True
  except Exception as e:
    print('제출 입력 에러:', e)
    return False


def press_space(driver):
  """스페이스를 보낸다. body를 먼저 클릭해 포커스를 다시 잡지 않으면
  키가 그대로 사라진다."""
  try:
    driver.find_element(By.TAG_NAME, 'body').click()
    time.sleep(0.15)
    ActionChains(driver).send_keys(Keys.SPACE).perform()
    dispatch_key(driver, ' ')
    return True
  except Exception as e:
    print('스페이스 입력 에러:', e)
    return False


def find_button_by_text(driver, labels, patterns=None):
  """labels 중 하나와 텍스트가 정확히 같거나 patterns에 맞는, 화면에
  보이는 요소를 돌려준다. 없으면 None."""
  try:
    return driver.execute_script(_BUTTON_BY_TEXT_JS, labels, patterns or [])
  except Exception:
    return None


def click_button_by_text(driver, labels, patterns=None, wait_after=1.2):
  """해당 버튼이 보이면 눌러서 True를 돌려준다. 클릭이 막히면 스페이스로
  대체한다(이런 화면에는 항상 SPACE 안내가 같이 붙어 있다).

  wait_after만큼 기다렸다 다음 일을 한다."""
  btn = find_button_by_text(driver, labels, patterns)
  if not btn:
    return False

  print(f'[DEBUG] 버튼 감지 - 클릭: {labels}')
  moved = False
  try:
    btn.click()
    moved = True
  except Exception:
    moved = press_space(driver)

  if moved:
    # 다음 화면이 뜰 때까지 대기. 통째로 자면 [정지]가 안 먹으므로 쪼갠다.
    # (is_running만 보다가, 자동 학습이 워커를 띄우기 전 단계에서는 한 번도
    #  안 기다려서 버튼을 1초에 수십 번씩 누른 적이 있다)
    for _ in range(int(wait_after / 0.1)):
      if not (is_running or auto_running):
        break
      time.sleep(0.1)
  return moved


def _spell_next_card(driver, timeout=3.0):
  """스펠에서 답을 낸 뒤 [다음 카드]가 나오면 바로 눌러 넘어간다.

  화면을 0.2초 간격으로 재보니 이렇게 흘러간다(2026-09-24 실측).
    0.0초 ~ 1.4초  큰 버튼이 아직 [확인/건너뛰기] - 스페이스를 보내도 씹힌다
    1.5초쯤        버튼이 **[다음 카드]**로 바뀐다 - 이때부터 넘어갈 수 있다
    (사이트가 저절로 넘어가지는 않는다. 6.9초를 그냥 두고 봤는데 그대로였다.)

  그래서 '언제쯤 되겠지' 하고 쉬는 대신, 버튼이 나타나는 순간을 지켜보다가
  바로 누른다. 이보다 빨리는 사이트가 안 넘겨준다.

  여기서는 버튼만 본다(가벼운 확인 한 번). 화면 글자를 매번 읽으면 그게
  0.3초씩 걸려서, 정작 버튼이 떠도 늦게 알아챘다."""
  end = time.time() + timeout
  while time.time() < end:
    if not (is_running or auto_running):
      return False
    btn = find_button_by_text(driver, _NEXT_CARD_LABELS)
    if btn:
      try:
        btn.click()
      except Exception:
        press_space_now(driver)
      time.sleep(0.15)   # 다음 카드가 그려질 짬
      return True
    time.sleep(0.05)
  return False


def _wait_next_sentence(driver, solved_key, timeout=1.5):
  """문장을 하나 맞힌 뒤, 다음 화면이 나오는 순간 바로 움직인다.

  둘 중 먼저 오는 쪽에 반응한다.
   - '문장 확인' 화면의 [영작 연습하기] -> 스페이스로 바로 연다.
   - 다음 문제의 조각 -> 아무것도 안 하고 바로 돌아가서 푼다.

  예전에는 [영작 연습하기]만 최대 2초까지 기다렸다. 그 화면 없이 다음
  문제가 바로 나오는 경우에는 그 2초를 문장마다 통째로 버렸고, 그래서
  '시작하면 계속 몇 초 있다가 누른다'는 말이 나왔다. 방금 푼 문장의 조각은
  화면에 잠깐 남아 있으므로, 조각이 '방금 푼 것과 다를 때'만 다음 문제로
  본다."""
  end = time.time() + timeout
  while time.time() < end:
    if not (is_running or auto_running):
      return
    try:
      # 카드를 맞히면 'Good Job!'과 함께 [한번 더] [다음 카드]만 뜨는 화면이
      # 있다(낱말이 하나뿐인 카드 등). 여기서는 스페이스가 안 먹어서,
      # [다음 카드]를 안 누르면 영영 그 자리에 멈춰 있었다.
      # ([한번 더]는 절대 누르면 안 된다 - 같은 카드를 다시 한다.)
      btn = find_button_by_text(driver, _NEXT_CARD_LABELS)
      if btn:
        print('[DEBUG] Good Job 화면 - [다음 카드] 누름')
        try:
          btn.click()
        except Exception:
          press_space(driver)
        time.sleep(0.15)
        return
      if find_button_by_text(driver, _WRITE_PRACTICE_LABELS):
        press_space(driver)
        # 화면이 실제로 넘어갈 때까지만 잠깐 본다. 이걸 안 보면 바깥
        # 반복문이 곧바로 같은 버튼을 또 찾아서 스페이스를 두 번 보낸다.
        gone = time.time() + 0.6
        while time.time() < gone:
          if not find_button_by_text(driver, _WRITE_PRACTICE_LABELS):
            break
          time.sleep(0.06)
        return
      groups = read_scramble_groups(driver)
      if groups:
        items = _usable_items(groups[0])
        if items:
          key = tuple(sorted(_norm_token(t) for _, t in items))
          if key != solved_key:
            return   # 다음 문제가 벌써 떠 있다
    except Exception:
      pass
    time.sleep(0.05)


def handle_section_done(driver):
  """구간/세트 완료 화면이면 다음으로 넘기고 True를 돌려준다.

  [다음 구간으로 이동]을 누른 뒤 1.5초 두고 다음 문제로 넘어간다. 바로
  이어서 진행하면 너무 빨라서 따라가기 어려웠다."""
  if auto_running:
    # 세트를 다 끝낸 화면('200% 도전' 또는 [계속하기])이면 여기서 이 모드를
    # 마치고 다음 모드로 넘어간다. 누르면 같은 세트를 한 바퀴 더 돌게 된다.
    if find_button_by_text(driver, _SECTION_AGAIN_LABELS, _SECTION_DONE_PATTERNS):
      auto_mode_done.set()
      return False
    # 테스트를 다 봤으면 여기서 끝낸다(문장 테스트는 [완료]만 뜬다).
    if on_class_test(driver) and find_button_by_text(driver, _TEST_DONE_LABELS):
      auto_mode_done.set()
      return False
    # 구간 하나가 끝난 것뿐이면 이어서 다음 구간으로 간다(같은 한 바퀴).
    return click_button_by_text(driver, _SECTION_NEXT_LABELS, wait_after=1.5)
  return click_button_by_text(
      driver, _SECTION_DONE_LABELS, _SECTION_DONE_PATTERNS, wait_after=1.5
  )


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
    # 시작하자마자, 화면에 열린 세트와 불러온 단어가 같은지 맞춰본다.
    ensure_word_list_for_screen(driver, min_gap=0)

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
        if pressed:
          _auto_card_done(current_word)

        if not pressed and is_running:
          # 구간 완료 화면인데도 지난 카드 단어가 화면에 남아 있어(스펠에서
          # 실측: young, child) 새 문제로 착각하고 계속 답만 찾다가 멈추는
          # 일이 있었다. 선택이 안 되면 완료/다음 카드 버튼을 본다.
          if handle_section_done(driver) or click_button_by_text(
              driver, _NEXT_CARD_LABELS, wait_after=0.4
          ):
            last_seen_eng = current_word['eng']
            continue

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
        # 다른 탭에서 학습 화면을 열었으면 그 탭으로 옮긴다. 화면을 못 읽고
        # 있을 때만 확인하므로 평소 속도에는 영향이 없다.
        focus_study_tab(driver, '/recall/')
        # 구간 완료 화면이면 여기서 다음 구간으로 넘어간다.
        if handle_section_done(driver):
          continue
        # 답을 고른 뒤 [다음 카드]를 기다리는 화면이면 눌러서 넘어간다.
        if click_button_by_text(
            driver, _NEXT_CARD_LABELS, wait_after=0.4
        ):
          continue
        # 화면에 열린 세트와 불러온 단어가 다르면 여기서 맞춘다.
        if ensure_word_list_for_screen(driver):
          continue
        if warn_if_no_cards(driver):
          time.sleep(1.0)
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
    # 시작하자마자, 화면에 열린 세트와 불러온 단어가 같은지 맞춰본다.
    ensure_word_list_for_screen(driver, min_gap=0)

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
            input_el.send_keys(Keys.RETURN)
            # 채점 -> 다음 카드가 뜨는 순간 바로 이어서 푼다.
            if not _spell_next_card(driver):
              press_space_now(driver)
            typed = True
          except StaleElementReferenceException:
            pass
          except Exception as e:
            print('입력 에러:', e)
        print(f"[DEBUG] 입력성공={typed}")
        if typed:
          _auto_card_done(current_word)

        if not typed and is_running:
          # 구간 완료 화면('GOOD JOB!! 구간 학습이 완료되었습니다')인데도
          # 지난 카드의 단어가 .spell-content에 그대로 남아 있어서(실측:
          # young, child가 계속 보임) 새 문제로 착각하고 입력창만 찾다가
          # 영영 멈춰 있었다. 입력이 안 되면 완료/다음 카드 버튼을 본다.
          if handle_section_done(driver) or click_button_by_text(
              driver, _NEXT_CARD_LABELS, wait_after=0.4
          ):
            last_seen_eng = current_word['eng']
            continue

        if typed and is_running:
          last_seen_eng = current_word['eng']
          root.after(
              0,
              lambda t=target_kor: lbl_status.config(
                  text=f"'{t}' 입력 완료! 다음 문제 이동",
                  fg='#388E3C',
              ),
          )
      else:
        # 다른 탭에서 학습 화면을 열었으면 그 탭으로 옮긴다. 화면을 못 읽고
        # 있을 때만 확인하므로 평소 속도에는 영향이 없다.
        focus_study_tab(driver, '/spell/')
        # 구간 완료 화면이면 여기서 다음 구간으로 넘어간다.
        if handle_section_done(driver):
          continue
        # 답을 고른 뒤 [다음 카드]를 기다리는 화면이면 눌러서 넘어간다.
        if click_button_by_text(
            driver, _NEXT_CARD_LABELS, wait_after=0.4
        ):
          continue
        # 화면에 열린 세트와 불러온 단어가 다르면 여기서 맞춘다.
        if ensure_word_list_for_screen(driver):
          continue
        if warn_if_no_cards(driver):
          time.sleep(1.0)
          continue
        root.after(
            0,
            lambda: lbl_status.config(
                text='스펠 학습 화면을 기다리는 중...', fg='gray'
            ),
        )

      # 방금 카드를 푼 직후에는 다음 문제를 이미 확인했으므로 안 쉰다.
      time.sleep(0.02 if current_word else 0.3)

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


_ON_SCREEN_JS = r"""
// 셀렉터에 맞는 요소 중 '지금 실제로 화면에 떠 있는 것'만 돌려준다.
// checkVisibility는 화면 밖으로 밀려난 카드나 다른 카드 뒤에 깔린 카드도
// 보인다고 판정한다. 테스트 화면은 다음 카드들을 미리 그려두는데, 그 카드의
// 문제/보기를 읽고 클릭하는 바람에 실제 화면에선 아무 일도 안 일어났다.
// 그래서 (1) 요소 가운데가 창 안에 있고 (2) 그 지점에서 맨 위에 있는 요소가
// 이 요소 자신이거나 그 안팎(부모/자식)일 때만 화면에 떠 있다고 본다.
function clippedAway(el, cx, cy) {
  // 부모 중 overflow로 내용을 자르는 상자가 있고, 요소 가운데가 그 상자 밖이면
  // 실제로는 안 보이는 것이다(옆에 대기 중인 다음 카드가 이렇게 숨어 있다).
  for (var a = el.parentElement; a && a !== document.body; a = a.parentElement) {
    var cs = getComputedStyle(a);
    if (/(hidden|clip|scroll|auto)/.test(cs.overflow + cs.overflowX + cs.overflowY)) {
      var ar = a.getBoundingClientRect();
      if (cx < ar.left || cx > ar.right || cy < ar.top || cy > ar.bottom) return true;
    }
  }
  return false;
}

function hitOk(el, cx, cy) {
  var hit = document.elementFromPoint(cx, cy);
  if (!hit) return false;
  if (hit === el || el.contains(hit)) return true;
  // 부모가 맞은 경우는 인정하되 body/html은 안 된다. 아무것도 안 그려진
  // 자리(잘린 영역)에서는 body가 맞는데, body는 모든 요소를 품고 있어서
  // 이걸 인정하면 가려진 카드까지 통과해버렸다.
  return hit !== document.body && hit !== document.documentElement &&
         hit.contains(el);
}

var sel = arguments[0];
var hitTest = arguments[1] !== false;
var W = window.innerWidth || document.documentElement.clientWidth;
var H = window.innerHeight || document.documentElement.clientHeight;
var out = [];
var nodes = document.querySelectorAll(sel);
for (var i = 0; i < nodes.length; i++) {
  var el = nodes[i];
  if (typeof el.checkVisibility === 'function') {
    try {
      if (!(function (e) {
      // 창을 내려놓거나 다른 탭을 보고 있으면(document.hidden) 크롬의
      // checkVisibility가 멀쩡히 그려진 것도 전부 false로 답한다.
      // 그럴 때는 크기와 조상 스타일로 직접 판단한다.
      if (!document.hidden) {
        try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); }
        catch (err) {}
      }
      var r = e.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return false;
      var n = e;
      while (n && n.nodeType === 1) {
        var cs = getComputedStyle(n);
        if (cs.display === "none" || cs.visibility === "hidden" ||
            parseFloat(cs.opacity) === 0) return false;
        n = n.parentElement;
      }
      return true;
    })(el)) continue;
    } catch (e) {}
  }
  var r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) continue;
  var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  if (cx < 0 || cx > W || cy < 0 || cy > H) continue;
  if (clippedAway(el, cx, cy)) continue;
  if (hitTest && !hitOk(el, cx, cy)) continue;
  var t = (el.textContent || '').trim();
  if (t) out.push({el: el, text: t});
}
return out;
"""


def read_on_screen(driver, selector, hit_test=True):
  """read_visible과 같지만, 화면 밖/다른 카드 뒤에 깔린 요소까지 걸러낸다.

  hit_test=False면 '창 안에 있는지'만 본다. 테스트 화면의 문제 단어는 위에
  투명한 층이 덮여 있는지 '그 자리 맨 위 요소' 검사를 통과하지 못해서, 눈앞의
  단어를 못 읽고 3초씩 기다렸다. 문제 단어는 뒤에 깔린 카드 것이 섞여도
  뜬 보기로 진짜 문제를 다시 확정하므로 이 검사 없이 읽는다."""
  try:
    raw = driver.execute_script(_ON_SCREEN_JS, selector, bool(hit_test)) or []
  except Exception:
    return []
  return [(d['el'], d['text']) for d in raw if d.get('text')]


_QUESTION_WORD_JS = r"""
// 테스트 단어 화면에서 지금 크게 떠 있는 단어를 찾는다.
// F12로 확인해보니 화면의 단어(예: office)는 클래스가 하나도 없는 요소였다.
// 매크로가 찾던 .cc-table...font- 요소는 카드 뒷면에 숨어 있는 복사본이라,
// 단어 화면에선 못 찾고 보기로 뒤집힌 뒤에야 찾았다.
// 그래서 클래스 대신 '화면에 실제로 보이는 글자 중 단어장에 있는 단어'를
// 찾는다. 보기 칸(.cc-table.middle.fill-parent) 안의 글자는 뺀다.
// 옆에 미리 대기 중인 다음 카드(예: actor)는 창 안에 있어도 실제로는 가려져
// 있으므로, 그 자리 맨 위 요소 검사(hitTest)로 걸러낸다.
function clippedAway(el, cx, cy) {
  // 부모 중 overflow로 내용을 자르는 상자가 있고, 요소 가운데가 그 상자 밖이면
  // 실제로는 안 보이는 것이다(옆에 대기 중인 다음 카드가 이렇게 숨어 있다).
  for (var a = el.parentElement; a && a !== document.body; a = a.parentElement) {
    var cs = getComputedStyle(a);
    if (/(hidden|clip|scroll|auto)/.test(cs.overflow + cs.overflowX + cs.overflowY)) {
      var ar = a.getBoundingClientRect();
      if (cx < ar.left || cx > ar.right || cy < ar.top || cy > ar.bottom) return true;
    }
  }
  return false;
}

function hitOk(el, cx, cy) {
  var hit = document.elementFromPoint(cx, cy);
  if (!hit) return false;
  if (hit === el || el.contains(hit)) return true;
  // 부모가 맞은 경우는 인정하되 body/html은 안 된다. 아무것도 안 그려진
  // 자리(잘린 영역)에서는 body가 맞는데, body는 모든 요소를 품고 있어서
  // 이걸 인정하면 가려진 카드까지 통과해버렸다.
  return hit !== document.body && hit !== document.documentElement &&
         hit.contains(el);
}

var vocab = arguments[0] || {};
var hitTest = arguments[1] !== false;
var W = window.innerWidth || document.documentElement.clientWidth;
var H = window.innerHeight || document.documentElement.clientHeight;
var out = [];
var nodes = document.querySelectorAll('body *');
for (var i = 0; i < nodes.length; i++) {
  var el = nodes[i];
  if (el.children.length) continue;
  var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
  if (!t || t.length > 80) continue;
  if (!vocab[t] && !vocab[t.toLowerCase()]) continue;
  if (el.closest && el.closest('.cc-table.middle.fill-parent')) continue;
  if (typeof el.checkVisibility === 'function') {
    try {
      if (!(function (e) {
      // 창을 내려놓거나 다른 탭을 보고 있으면(document.hidden) 크롬의
      // checkVisibility가 멀쩡히 그려진 것도 전부 false로 답한다.
      // 그럴 때는 크기와 조상 스타일로 직접 판단한다.
      if (!document.hidden) {
        try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); }
        catch (err) {}
      }
      var r = e.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return false;
      var n = e;
      while (n && n.nodeType === 1) {
        var cs = getComputedStyle(n);
        if (cs.display === "none" || cs.visibility === "hidden" ||
            parseFloat(cs.opacity) === 0) return false;
        n = n.parentElement;
      }
      return true;
    })(el)) continue;
    } catch (e) {}
  }
  var r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) continue;
  var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  if (cx < 0 || cx > W || cy < 0 || cy > H) continue;
  if (clippedAway(el, cx, cy)) continue;
  if (hitTest && !hitOk(el, cx, cy)) continue;
  if (out.indexOf(t) === -1) out.push(t);
}
return out;
"""


def _test_vocab():
  """_QUESTION_WORD_JS에 넘길 단어장 글자 모음 {글자: True}."""
  vocab = {}
  for w in word_list:
    eng = (w.get('eng') or '').strip()
    kor = (w.get('kor') or '').strip()
    for key in (eng.lower(), kor, _strip_pos_tag(kor)):
      if key:
        vocab[key] = True
  return vocab


def read_question_words(driver, vocab, hit_test=True):
  """테스트 단어 화면에 떠 있는 단어(단어장에 있는 것만)를 돌려준다."""
  try:
    return driver.execute_script(_QUESTION_WORD_JS, vocab, bool(hit_test)) or []
  except Exception:
    return []


def _choice_digit(choice_el):
  """보기를 감싼 <label for="radio_0_N">에서 N을 읽는다. 없으면 None.

  단어장에 따라 이 번호 체계가 화면 번호와 안 맞는 경우가 있어서(보카클리어
  테스트는 2x3 격자로 번호가 따로 붙어 있다) 보조 수단으로만 쓴다."""
  try:
    label_el = choice_el.find_element(By.XPATH, './ancestor::label[1]')
    digit = (label_el.get_attribute('for') or '').rsplit('_', 1)[-1]
    return digit if digit.isdigit() else None
  except Exception:
    return None


_REFOCUS_JS = r"""
// 키 입력이 페이지로 가도록 포커스만 정리한다. 예전엔 body를 클릭했는데,
// 셀레니움 클릭은 요소 한가운데를 누르므로 보기 화면에서는 가운데 있는 보기
// 칸이 눌려 엉뚱한 답이 골라질 수 있었다.
var a = document.activeElement;
if (a && a !== document.body && typeof a.blur === 'function') {
  try { a.blur(); } catch (e) {}
}
window.focus();
"""


_CHOICE_TAKEN_JS = r"""
// 보기가 실제로 선택됐는지 본다. 라디오가 체크됐거나, 보기 화면이 사라졌거나
// (요소가 없어짐/안 보임/글자가 바뀜) 하면 선택된 것으로 본다.
var el = arguments[0], txt = arguments[1];
if (!el || !el.isConnected) return true;
var l = el.closest ? el.closest('label') : null;
if (l && l.control && l.control.checked) return true;
if ((el.textContent || '').trim() !== txt) return true;
if (typeof el.checkVisibility === 'function') {
  try {
    if (!(function (e) {
      // 창을 내려놓거나 다른 탭을 보고 있으면(document.hidden) 크롬의
      // checkVisibility가 멀쩡히 그려진 것도 전부 false로 답한다.
      // 그럴 때는 크기와 조상 스타일로 직접 판단한다.
      if (!document.hidden) {
        try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); }
        catch (err) {}
      }
      var r = e.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return false;
      var n = e;
      while (n && n.nodeType === 1) {
        var cs = getComputedStyle(n);
        if (cs.display === "none" || cs.visibility === "hidden" ||
            parseFloat(cs.opacity) === 0) return false;
        n = n.parentElement;
      }
      return true;
    })(el)) return true;
  } catch (e) {}
}
var r = el.getBoundingClientRect();
return r.width <= 0 || r.height <= 0;
"""


def _choice_taken(driver, choice_el, text):
  try:
    return bool(driver.execute_script(_CHOICE_TAKEN_JS, choice_el, text))
  except StaleElementReferenceException:
    return True
  except Exception:
    return False


def _select_test_choice(driver, choice_el, digit, text):
  """테스트 보기를 고른다. 실제로 선택된 게 확인되면 True.

  숫자 키가 기본이다. 화면 녹화로 확인해보니 보카클리어 테스트 화면은
  셀레니움 클릭으로는 선택이 안 됐고(마우스가 올라간 표시만 뜨고 시간 초과),
  번호 속성(radio_0_N)은 화면 번호와 정확히 맞았다(농부=6, 사무실=1).

  예전엔 키를 누르기만 하고 성공으로 쳤다. 그런데 보기가 막 뜬 직후(카드가
  뒤집히는 중)에 누르면 씹혀서, 로그는 '매칭성공=True'인데 실제로는 선택이
  안 됐고 그 보기 화면이 남아 다음 문제 스페이스까지 줄줄이 안 먹었다.
  그래서 누를 때마다 선택됐는지 확인하고, 안 됐으면 다시 누른다
  (숫자키 3번 -> 클릭 -> JS클릭 -> 숫자키 순)."""
  def digit_key():
    if not digit:
      raise ValueError('번호 속성 없음')
    driver.execute_script(_REFOCUS_JS)
    time.sleep(0.05)
    ActionChains(driver).send_keys(digit).perform()
    dispatch_key(driver, digit)

  def native_click():
    choice_el.click()

  def js_click():
    driver.execute_script(_CLICK_CHIP_JS, choice_el)

  plan = [('숫자키', digit_key)] * 3 + [
      ('클릭', native_click), ('JS클릭', js_click)] + [('숫자키', digit_key)] * 2
  for n, (name, action) in enumerate(plan):
    if not is_running:
      return False
    if _choice_taken(driver, choice_el, text):
      return True
    try:
      action()
    except StaleElementReferenceException:
      return True
    except Exception as e:
      print(f'[DEBUG] 보기 {name} 실패: {str(e).splitlines()[0][:80]}')
      continue
    # 로그상 첫 번째 숫자키는 매번 씹히고(보기가 막 뜬 직후) 두 번째에 먹었다.
    # 선택되면 라디오가 바로 체크되므로 오래 볼 필요 없이 0.25초만 보고
    # 안 됐으면 바로 다시 누른다.
    for _ in range(5):
      time.sleep(0.05)
      if _choice_taken(driver, choice_el, text):
        if n:
          print(f'[DEBUG] 보기 선택 확인 ({n + 1}번째 시도, {name})')
        return True
    print(f'[DEBUG] 보기 {name} 눌렀는데 선택 안 됨 ({n + 1}번째)')
  return False


_WAIT_WORD_SCREEN_JS = r"""
// 답을 고른 뒤 '다음 단어 화면'이 뜨는 순간을 브라우저 안에서 5ms 간격으로
// 지켜본다. 파이썬에서 0.1초마다 확인하던 것보다 훨씬 빨리 알아챈다.
// 조건: 단어 칸(방금 푼 단어가 아닌 것)이 화면에 있고, 보기는 화면에 없음.
var wordSel = arguments[0], choiceSel = arguments[1];
var exclude = arguments[2] || [], timeoutMs = arguments[3] || 3000;
var done = arguments[arguments.length - 1];
function clippedAway(el, cx, cy) {
  for (var a = el.parentElement; a && a !== document.body; a = a.parentElement) {
    var cs = getComputedStyle(a);
    if (/(hidden|clip|scroll|auto)/.test(cs.overflow + cs.overflowX + cs.overflowY)) {
      var ar = a.getBoundingClientRect();
      if (cx < ar.left || cx > ar.right || cy < ar.top || cy > ar.bottom) return true;
    }
  }
  return false;
}
function hitOk(el, cx, cy) {
  var hit = document.elementFromPoint(cx, cy);
  if (!hit) return false;
  if (hit === el || el.contains(hit)) return true;
  return hit !== document.body && hit !== document.documentElement && hit.contains(el);
}
function onScreen(sel, hitTest) {
  var W = window.innerWidth || document.documentElement.clientWidth;
  var H = window.innerHeight || document.documentElement.clientHeight;
  var out = [], nodes = document.querySelectorAll(sel);
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    if (typeof el.checkVisibility === 'function') {
      try {
        if (!(function (e) {
      // 창을 내려놓거나 다른 탭을 보고 있으면(document.hidden) 크롬의
      // checkVisibility가 멀쩡히 그려진 것도 전부 false로 답한다.
      // 그럴 때는 크기와 조상 스타일로 직접 판단한다.
      if (!document.hidden) {
        try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); }
        catch (err) {}
      }
      var r = e.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return false;
      var n = e;
      while (n && n.nodeType === 1) {
        var cs = getComputedStyle(n);
        if (cs.display === "none" || cs.visibility === "hidden" ||
            parseFloat(cs.opacity) === 0) return false;
        n = n.parentElement;
      }
      return true;
    })(el)) continue;
      } catch (e) {}
    }
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cx > W || cy < 0 || cy > H) continue;
    if (clippedAway(el, cx, cy)) continue;
    if (hitTest && !hitOk(el, cx, cy)) continue;
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (t) out.push(t);
  }
  return out;
}
// 같은 단어가 연달아 다시 출제되기도 한다(로그: scientist 두 번 -> 3초
// 기다림). 그래서 보기가 사라진 뒤 0.25초가 지나도 방금 푼 단어만 보이면
// 그 단어가 다시 나온 것으로 본다.
var start = Date.now(), goneAt = 0;
(function tick() {
  try {
    if (onScreen(choiceSel, true).length) {
      goneAt = 0;
    } else {
      if (!goneAt) goneAt = Date.now();
      var all = onScreen(wordSel, false);
      var words = all.filter(function (t) { return exclude.indexOf(t) === -1; });
      if (!words.length && all.length && Date.now() - goneAt > 250) words = all;
      if (words.length) {
        done({ok: true, words: words});
        return;
      }
    }
  } catch (e) {}
  if (Date.now() - start > timeoutMs) { done({ok: false, words: []}); return; }
  setTimeout(tick, 5);
})();
"""


def wait_word_screen(driver, word_sel, choice_sel, exclude, timeout=3.0):
  """다음 단어 화면이 뜰 때까지 기다린다. 뜨면 그 단어들, 아니면 None."""
  try:
    driver.set_script_timeout(timeout + 2)
    res = driver.execute_async_script(
        _WAIT_WORD_SCREEN_JS, word_sel, choice_sel, list(exclude),
        int(timeout * 1000)) or {}
  except Exception:
    return None
  return res.get('words') if res.get('ok') else None


def press_space_now(driver):
  """기다림 없이 바로 스페이스를 누른다."""
  driver.execute_script(_REFOCUS_JS)
  ActionChains(driver).send_keys(Keys.SPACE).perform()
  dispatch_key(driver, ' ')


def _build_test_candidates(texts, seen_keys):
  """화면에서 읽은 문제 글자들을 [(글자, 단어, 방향, 정답), ...]으로 바꾼다.

  이미 푼 문제와 단어장에 없는 글자는 뺀다. 영단어가 뜨면 한글 뜻을,
  한글 뜻이 뜨면 영단어를 고르는 문제다(테스트는 중간에 방향이 바뀐다)."""
  out = []
  for text in texts:
    cand = (text or '').strip()
    if not cand or cand in seen_keys or any(cand == c[0] for c in out):
      continue
    for w in word_list:
      if w['eng'].strip().lower() == cand.lower():
        out.append((cand, w, 'eng_to_kor', w['kor'].strip()))
        break
      if _strip_pos_tag(w['kor']) == cand or w['kor'].strip() == cand:
        out.append((cand, w, 'kor_to_eng', w['eng'].strip()))
        break
  return out


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
  # 화면에 떠 있는지를 직접 확인할 때는 카드 클래스로 좁히지 않는다. 단어장에
  # 따라(보카클리어) 단어 화면일 때는 그 카드에 '현재' 클래스가 아직 안 붙어
  # 있어서, 좁히면 눈앞의 단어를 3초씩 못 읽었다. 화면 밖/뒤에 깔린 카드는
  # read_on_screen이 따로 걸러낸다.
  ON_SCREEN_QUESTION_SELECTOR = FALLBACK_QUESTION_SELECTOR
  CHOICE_SELECTOR = '.cc-table.middle.fill-parent:not([class*="font-"])'
  # 사용자가 F12로 확인한 단어 화면의 단어 칸(클래스 없음, style만 있음):
  # <div style="max-height: 406px; overflow: hidden; display: block;
  #   text-align: center; height: 51px;">do a good job</div>
  # 이게 화면에 떠 있고 보기가 없으면 '단어 확인 화면'이므로 바로 스페이스.
  WORD_BOX_SELECTOR = 'div[style*="text-align: center"][style*="overflow: hidden"]'
  SCOPED_CHOICE_SELECTOR = f'{CURRENT_CARD} {CHOICE_SELECTOR}'

  try:
    driver = get_driver()
    # 시작하자마자, 화면에 열린 세트와 불러온 단어가 같은지 맞춰본다.
    ensure_word_list_for_screen(driver, min_gap=0)

    root.after(
        0,
        lambda: lbl_status.config(
            text='테스트 학습 화면 감지 대기 중...', fg='#2E7D32'
        ),
    )

    seen_keys = set()
    vocab = _test_vocab()
    fail_counts = {}
    last_logged_qtext = None
    empty_streak = 0
    # 답을 고른 시각. 다음 문제에서 스페이스를 누르기까지 몇 초 걸렸는지
    # 로그에 남겨서, 가끔 늦어질 때 어느 단계에서 막히는지 본다.
    answered_at = None
    stuck_streak = 0
    # 답을 고른 뒤 다음 단어 화면을 바로 알아채서 이미 스페이스를 눌렀는지,
    # 그때 화면에 떠 있던 단어들.
    space_pressed_early = False
    pending_words = []

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
      q_candidates = read_question_words(driver, vocab)
      early_words = pending_words
      if pending_words:
        q_candidates = list(pending_words) + [
            t for t in q_candidates if t not in pending_words]
        pending_words = []
      if q_candidates:
        empty_streak = 0
      else:
        empty_streak += 1
        # 화면에서 문제를 못 찾을 때의 백업. 가시성 판단 없이 읽어서 미리
        # 그려진 다른 카드의 문제까지 끌려오지만, 스페이스를 누른 뒤 뜬 보기로
        # 진짜 문제를 다시 확정하므로 틀린 답으로 이어지진 않는다. 테스트는
        # 문제마다 제한 시간이 있어서 오래 기다리면 시간 초과가 나므로
        # 짧게(약 0.6초) 기다린다.
        if empty_streak >= 3:
          if empty_streak == 3:
            print('[DEBUG] 화면에서 문제를 못 찾아 백업 방식으로 읽음')
          q_candidates = read_question_words(driver, vocab, hit_test=False)
          if not q_candidates:
            q_candidates = read_all_texts(driver, QUESTION_SELECTOR)

      if q_candidates != last_logged_qtext:
        since = f' (답 고른 뒤 {time.time() - answered_at:.1f}초)' if answered_at else ''
        print(f"[DEBUG] 화면에 실제로 보이는 문제 텍스트들={q_candidates}{since}")
        last_logged_qtext = q_candidates

      # 문제 후보가 여럿 잡힐 수 있다(카드가 넘어가는 동안 다른 카드의 단어가
      # 같이 보임). 여기서 하나로 정하지 않고 전부 모아뒀다가, 스페이스를
      # 누른 뒤 실제로 뜬 보기를 보고 '정답이 그 보기 안에 있는 후보'를 진짜
      # 문제로 확정한다. 예전엔 첫 후보를 바로 골라서, ['actor', '간호사']
      # 중 '간호사'(미리 그려진 다른 카드)를 문제로 착각하고 'job'의 보기에서
      # 'nurse'를 찾다가 시간을 다 썼다.
      # 단어 칸에서 읽은 글자도 문제 후보에 더한다.
      word_boxes = [
          ' '.join(t.split()) for _, t in read_on_screen(driver, WORD_BOX_SELECTOR)
      ]
      for t in word_boxes:
        if t not in q_candidates:
          q_candidates = list(q_candidates) + [t]

      candidates = _build_test_candidates(q_candidates, seen_keys)
      if early_words:
        # 이미 스페이스를 눌렀으므로 그때 뜬 단어는 이미 푼 단어라도(같은 단어
        # 재출제) 후보에 넣는다. 안 그러면 보기 화면에서 문제를 못 정해 멈춘다.
        for c in _build_test_candidates(early_words, set()):
          if not any(c[0] == x[0] for x in candidates):
            candidates.insert(0, c)

      # 단어 화면인데 '새' 단어를 못 찾고 방금 푼 단어만 보이는 경우가 있다
      # (로그: 새 단어 옆에 방금 푼 단어가 늘 같이 잡힘 ['job', 'want']).
      # 예전엔 이때 '아직 새 문제가 안 떴다'며 스페이스를 안 누르고 계속
      # 기다렸다. 같은 단어가 시험에 다시 나올 때도 똑같이 멈췄다. 보기가 안
      # 떠 있는데(=단어 화면) 잠깐(약 0.3초) 이러면, 푼 단어까지 후보에 넣고
      # 스페이스를 누른다. 진짜 문제는 뜬 보기를 보고 다시 확정한다.
      if candidates:
        stuck_streak = 0
      elif is_running:
        stuck_streak += 1
        # 단어 칸이 떠 있으면 기다리지 않고 바로, 아니면 약 0.3초 뒤에.
        if (word_boxes or stuck_streak >= 3) and not read_on_screen(
            driver, CHOICE_SELECTOR):
          pool = q_candidates or read_question_words(driver, vocab, hit_test=False)
          candidates = _build_test_candidates(pool, set())
          if candidates:
            print(f"[DEBUG] 새 단어를 못 찾아 이미 푼 단어 포함해서 진행: {pool}")

      if candidates and is_running:
        current_cand_norm, current_word, direction, target_answer = candidates[0]
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
        # 스페이스를 누르기 전에 이미 보이던 보기는 '이전 문제'의 것이다.
        # 두 번째 단어부터 스페이스가 안 먹는 것처럼 보였던 원인이 이것 -
        # 앞 카드가 넘어가는 동안 그 보기가 아직 화면에 남아 있어서, 스페이스를
        # 누른 직후 그걸 '보기가 떴다'로 착각하고 번호를 눌렀다(실제로는 아직
        # 단어 확인 화면). 이 사이트는 카드마다 요소를 따로 만들므로, 누르기
        # 전 요소는 빼고 새로 나타난 보기만 본다.
        # 단, 보카클리어 단어장은 다음 카드의 보기를 같은 칸에 글자만 바꿔서
        # 띄운다(로그: nurse 직후 '이전 보기 6개 제외'인데 보기=[]만 25번).
        # 그래서 칸이 같아도 글자가 바뀌었으면 새 보기로 본다.
        # 앞 카드 보기가 아직 화면에 남아 있으면(카드가 넘어가는 중) 그게 다
        # 사라질 때까지 기다렸다가 누른다. 로그상 매 카드 '이전 보기 3개 제외'
        # 상태에서 누른 첫 스페이스가 씹혀서, 1초 뒤 재시도로 넘어갔다.
        # 이미 스페이스를 눌렀으면 지금 뜬 보기가 새 보기이므로 기다리지 않는다.
        stale = [] if space_pressed_early else read_on_screen(driver, CHOICE_SELECTOR)
        waited = 0
        while stale and waited < 20 and is_running:
          time.sleep(0.05)
          waited += 1
          stale = read_on_screen(driver, CHOICE_SELECTOR)
        if waited:
          print(f"[DEBUG] 앞 카드 보기가 사라지길 {waited * 0.05:.2f}초 기다림 (남은 보기 {len(stale)}개)")

        if answered_at:
          print(f"[DEBUG] 스페이스 누름: 문제='{current_cand_norm}' "
                f"(답 고른 뒤 {time.time() - answered_at:.1f}초)")
          answered_at = None
        choices, attempt = [], 0
        choice_scope = '-'
        # 첫 스페이스가 씹히면 보기가 뜰 때까지 빠르게 연타한다(사용자 요청).
        # 보기가 하나라도 뜨면 바로 멈추므로 보기 화면에서 더 누르지는 않는다.
        # 최대 약 4초.
        for space_try in range(25):
          if not is_running:
            break
          # 페이지 가운데를 클릭하지 않고 포커스만 정리한다. 재시도할 때는
          # 이미 보기 화면일 수 있는데, 그때 가운데를 클릭하면 보기가 눌린다.
          if space_try or not space_pressed_early:
            press_space_now(driver)
          window = 0.35 if space_try == 0 else 0.12
          t0 = time.time()

          # 새 보기가 화면에 렌더링될 때까지 최대 약 1초 기다린다. 보기는 보통
          # 0.3초 안에 뜨는데, 스페이스가 씹힌 경우 2.5초씩 기다렸다가 다시
          # 눌러서 한 번씩 너무 늦었다(로그: vet).
          #
          # 보기도 문제처럼 현재 카드 안에서 먼저 찾는다. 범위를 안 좁히면
          # 미리 그려진 다른 카드의 보기를 먼저 잡는 일이 있었다('be proud
          # of' 문제에 '엔지니어, 일하다, 디자이너...' 보기 5개를 읽음).
          # 현재 카드에서 못 찾으면 범위 없이 찾는다.
          #
          # 또 읽은 보기에 정답이 없으면 바로 포기하지 않는다. 제대로 된
          # 보기가 조금 늦게 뜨는 경우가 있어서, 정답이 보일 때까지 계속
          # 다시 읽는다. (시간이 다 되면 그때 가진 보기로 판단한다)
          attempt = -1
          while True:
            attempt += 1
            if not is_running or time.time() - t0 > 1.0:
              break
            choices = []
            for sel in (SCOPED_CHOICE_SELECTOR, CHOICE_SELECTOR):
              choices = [
                  (el, t)
                  for el, t in read_on_screen(driver, sel)
                  if not any(el == old_el and t == old_t for old_el, old_t in stale)
              ]
              if choices:
                choice_scope = '현재카드' if sel == SCOPED_CHOICE_SELECTOR else '전체'
                break
            if choices:
              # 진짜 현재 문제가 이제서야 보일 수 있으므로 문제도 다시 읽는다.
              more = _build_test_candidates(
                  read_question_words(driver, vocab)
                  + [t for _, t in read_on_screen(
                      driver, ON_SCREEN_QUESTION_SELECTOR, hit_test=False)],
                  seen_keys,
              )
              for m in more:
                if not any(m[0] == c[0] for c in candidates):
                  candidates.append(m)
              if any(_pick_choice(choices, c[3]) for c in candidates):
                break
            elif time.time() - t0 > window:
              # 보기는 보통 0.3초 안에 뜬다. 그 안에 하나도 안 떴으면 스페이스가
              # 씹힌 것이니 바로 다시 누른다(이후로는 약 0.12초 간격 연타).
              break
            time.sleep(0.02)

          if choices or not is_running:
            break
          if space_try == 0:
            print("[DEBUG] 보기 화면이 안 떠서 스페이스 연타 시작")
        space_pressed_early = False
        if space_try:
          print(f"[DEBUG] 스페이스 {space_try + 1}번 눌러서 보기 뜸" if choices
                else f"[DEBUG] 스페이스 {space_try + 1}번 눌렀는데도 보기 안 뜸")

        # 뜬 보기로 진짜 문제를 확정한다.
        for c in candidates:
          if _pick_choice(choices, c[3]):
            if c[0] != current_cand_norm:
              print(f"[DEBUG] 보기를 보고 문제를 '{current_cand_norm}' -> '{c[0]}'로 바로잡음")
            current_cand_norm, current_word, direction, target_answer = c
            break

        debug_texts = [t for _, t in choices]
        print(
            f"[DEBUG] target_answer='{target_answer}' / 화면에 보이는 보기들={debug_texts} "
            f"(시도={attempt + 1}회, 이전 보기 {len(stale)}개 제외, 범위={choice_scope})"
        )

        pressed = False
        picked = _pick_choice(choices, target_answer)
        if picked and is_running:
          _, choice_el, matched_text = picked
          digit = _choice_digit(choice_el)
          print(f"[DEBUG] 고른 보기='{matched_text}' (번호 속성={digit})")
          pressed = _select_test_choice(driver, choice_el, digit, matched_text)
        print(f"[DEBUG] 매칭성공={pressed}")
        if pressed:
          _auto_card_done(current_word)

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
          # 고른 보기 화면이 사라질 때까지(최대 2초) 기다렸다가 다음 문제를
          # 읽는다. 바로 넘어가면 아직 떠 있는 이 보기를 다음 문제의 '이전
          # 보기'로 기억하거나, 사라지는 중인 화면에서 엉뚱한 글자를 문제로
          # 읽었다(nurse 직후 '모델, 모형'을 문제로 착각).
          #
          # 이제는 브라우저 안에서 5ms 간격으로 '다음 단어 칸이 뜨고 보기는
          # 사라졌는지' 지켜보다가, 뜨는 순간 바로 스페이스를 누른다(사용자
          # 요청: 단어 칸을 알아채면 밀리초 단위로 바로). 3초 안에 안 뜨면
          # 예전처럼 다음 반복에서 천천히 확인한다.
          answered_at = time.time()
          words = wait_word_screen(
              driver, WORD_BOX_SELECTOR, CHOICE_SELECTOR, [current_cand_norm])
          if words and is_running:
            press_space_now(driver)
            space_pressed_early = True
            pending_words = words
            print(f"[DEBUG] 단어 화면 감지 즉시 스페이스: {words} "
                  f"(답 고른 뒤 {time.time() - answered_at:.2f}초)")
            answered_at = None
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
        space_pressed_early = False
        # 다른 탭에서 학습 화면을 열었으면 그 탭으로 옮긴다. 화면을 못 읽고
        # 있을 때만 확인하므로 평소 속도에는 영향이 없다.
        focus_study_tab(driver, '/classtest/')
        # 구간 완료 화면이면 여기서 다음 구간으로 넘어간다.
        if handle_section_done(driver):
          continue
        # 답을 고른 뒤 [다음 카드]를 기다리는 화면이면 눌러서 넘어간다.
        if click_button_by_text(
            driver, _NEXT_CARD_LABELS, wait_after=0.4
        ):
          continue
        # 화면에 열린 세트와 불러온 단어가 다르면 여기서 맞춘다.
        if ensure_word_list_for_screen(driver):
          continue
        if warn_if_no_cards(driver):
          time.sleep(1.0)
          continue
        root.after(
            0,
            lambda: lbl_status.config(
                text='테스트 학습 화면을 기다리는 중...', fg='gray'
            ),
        )

      if not space_pressed_early:
        time.sleep(0.1)

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


_SCRAMBLE_JS = r"""
// 낱말 조각을 부모별로 묶어서 돌려준다. 화면마다 클래스가 다르다 -
// 암기(영작 연습)/스펠은 .scramble-item, 리콜(듣고 배열)은 .btn-scramble,
// 테스트는 클래스가 난독화돼 있다(c2NyYW1ibGUxNzg4MDU5MzMz = base64로
// scramble1788059333, 세션마다 바뀜). data-idx로 잡았더니 페이지의 강의
// 목록 링크까지 걸려서 엉뚱한 묶음을 보고 있었다. base64는 3글자씩 끊어
// 인코딩하므로 앞의 'scramb'에 해당하는 c2NyYW1i는 항상 같다.
// 이 사이트는 지난/다음 카드가 DOM에 그대로 남는 일이 잦아서(단어 모드에서
// 크게 데였다) 전체를 한 줄로 읽으면 다른 카드 조각이 섞인다. 부모가 다르면
// 다른 카드이므로 묶어두면 현재 카드만 골라낼 수 있다.
// 긴 문장은 조각이 한 줄에 안 들어가 뒷부분이 '...'로 잘려 보인다. 잘린
// 조각도 DOM에는 있으므로 전부 돌려주되, 보이는지(vis) / 누를 수 있는지(clk)
// / 화면 위치(top)를 같이 넘긴다. 이미 문장에 놓인 낱말도 같은 클래스라서
// 아래 조각 묶음과 구분해야 하는데, 이 셋으로 파이썬 쪽에서 걸러낸다.
var nodes = document.querySelectorAll(
    '.scramble-item, .btn-scramble, [class*="c2NyYW1i"]');
var parents = [];
var groups = [];
for (var i = 0; i < nodes.length; i++) {
  var el = nodes[i];
  var vis = true;
  if (typeof el.checkVisibility === 'function') {
    try {
      vis = (function (e) {
      // 창을 내려놓거나 다른 탭을 보고 있으면(document.hidden) 크롬의
      // checkVisibility가 멀쩡히 그려진 것도 전부 false로 답한다.
      // 그럴 때는 크기와 조상 스타일로 직접 판단한다.
      if (!document.hidden) {
        try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); }
        catch (err) {}
      }
      var r = e.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return false;
      var n = e;
      while (n && n.nodeType === 1) {
        var cs = getComputedStyle(n);
        if (cs.display === "none" || cs.visibility === "hidden" ||
            parseFloat(cs.opacity) === 0) return false;
        n = n.parentElement;
      }
      return true;
    })(el);
    } catch (e) {}
  }
  var r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) vis = false;
  var cs = getComputedStyle(el);
  var p = el.parentElement;
  var idx = parents.indexOf(p);
  if (idx === -1) {
    parents.push(p);
    groups.push([]);
    idx = groups.length - 1;
  }
  groups[idx].push({
    el: el,
    text: (el.textContent || '').trim(),
    vis: vis,
    clk: el.classList.contains('clickable'),
    // 이미 눌러서 문장에 들어간 조각은 자리만 빈 상자로 남는다. 글자는
    // 그대로라 안 걸러내면 남은 조각 수를 잘못 세게 된다. class에
    // 'clicked'가 붙고 글자 색이 투명해지는데, 테스트 화면은 클래스가
    // 난독화돼 있어 색까지 같이 본다.
    used: el.classList.contains('clicked') || /,\s*0\s*\)$/.test(cs.color),
    top: r.top
  });
}
return groups;
"""


_CLICK_CHIP_JS = r"""
// 조각을 누른다. el.click()만으로는 반응하지 않는 화면이 있어서(테스트
// 화면의 조각은 <a data-idx="0">라 마우스 이벤트를 직접 받는다) 실제
// 마우스 동작과 같은 순서로 이벤트를 보낸다.
//
// 클릭은 반드시 한 번만 보낸다. 예전엔 click 이벤트를 보낸 뒤 el.click()을
// 또 불러서 조각마다 두 번 눌렸다. 사이트 코드(spell_sentence.js)를 풀어보니
// 조각을 누르면 '지금 놓일 차례의 낱말인지'만 보고 아니면 즉시 오답(shake)
// 처리한다 - 이미 누른 조각인지는 안 본다. 그래서 두 번째 클릭이 '다음
// 낱말 자리에 앞 낱말을 누른 것'이 되어 문장 스펠이 전부 오답이 됐다.
var el = arguments[0];
var opts = {bubbles: true, cancelable: true, view: window, button: 0};
el.dispatchEvent(new MouseEvent('mouseover', opts));
el.dispatchEvent(new MouseEvent('mousedown', opts));
el.dispatchEvent(new MouseEvent('mouseup', opts));
el.dispatchEvent(new MouseEvent('click', opts));
"""


def click_chip(driver, el, native_first=False, debug=False, allow_js=True):
  """조각 하나를 누른다.

  기본은 자바스크립트로 요소에 직접 이벤트를 보낸다 - 셀레니움 클릭은
  좌표 기반이라, 조각을 누를 때마다 트레이가 다시 그려지면서 밀려온 다른
  조각을 누르는 일이 있었다. 그래도 반응이 없으면 셀레니움 클릭으로
  바꿔서 시도한다."""
  order = (True, False) if native_first else (False, True)
  if not allow_js:
    order = (True,)
  for native in order:
    name = '셀레니움' if native else 'JS'
    try:
      if native:
        el.click()
      else:
        driver.execute_script(_CLICK_CHIP_JS, el)
      if debug:
        print(f'[DEBUG] {name} 클릭 보냄')
      return True
    except StaleElementReferenceException:
      return False
    except Exception as e:
      print(f'[DEBUG] {name} 클릭 실패: {str(e).splitlines()[0][:120]}')
      continue
  return False


def read_scramble_groups(driver):
  """지금 화면에 떠 있는 카드의 낱말 조각을 [[(요소, 글자), ...], ...] 로.

  잘려서 안 보이는 조각도 포함한다 - 남은 낱말이 몇 개인지 정확히 알아야
  지금 눌러야 할 자리를 계산할 수 있다. 다만 이미 누른 조각(class에
  'clicked')과, 조각이 하나도 안 보이는 묶음(지난 카드)은 버린다."""
  try:
    raw = driver.execute_script(_SCRAMBLE_JS) or []
  except Exception:
    return []

  usable = []
  for group in raw:
    if not any(d.get('vis') for d in group):
      continue
    items = [
        (d['el'], d['text'])
        for d in group
        if d.get('text') and not d.get('used')
    ]
    if not items:
      continue
    tops = [d.get('top') or 0 for d in group]
    usable.append({
        'items': items,
        'clickable': all(d.get('clk') for d in group),
        'top': sum(tops) / len(tops),
    })

  if not usable:
    return []

  # 이미 문장에 놓인 낱말도 같은 클래스를 달고 있어서, 그쪽을 남은 조각으로
  # 착각하면 문장을 중간까지만 배열하고 끝난다. 누를 수 있는(clickable) 묶음이
  # 있으면 그것만 쓰고, 그래도 여럿이면 화면 아래쪽(조각 트레이)을 먼저 본다.
  clickable = [g for g in usable if g['clickable']]
  if clickable:
    usable = clickable
  usable.sort(key=lambda g: -g['top'])
  return [g['items'] for g in usable]


def _is_submultiset(chips, tokens):
  """chips의 낱말이 tokens 안에 (개수까지 포함해) 전부 들어있는지."""
  pool = list(tokens)
  for chip in chips:
    if chip in pool:
      pool.remove(chip)
    else:
      return False
  return True


def _usable_items(items):
  """조각 중 실제 낱말인 것만 남긴다. 긴 문장에서는 '...' 표시가 조각처럼
  같이 잡히는데, 정규화하면 빈 문자열이 되므로 걸러낸다."""
  return [(el, txt) for el, txt in items if _norm_token(txt)]


def match_scramble_sentence(groups):
  """조각을 보고 어떤 문장인지 역산한다. (요소들, 문장, 정답 순서) 반환.

  긴 문장은 조각을 앞부분만 보여주고 나머지는 '...'로 감춘다. 그래서
  '조각 구성 == 문장 전체'로 보면 긴 문장은 아예 매칭이 안 됐다. 보이는
  조각이 문장 낱말의 부분집합인지로 판단하고, 후보가 여럿이면 남는 낱말이
  가장 적은 쪽을 고른다. 그래도 동점이면 어느 문장인지 확신할 수 없으므로
  건드리지 않는다.

  화면에 한글 뜻이 같이 떠 있긴 하지만, 조각만으로 역산하면 한글 쪽
  셀렉터에 의존하지 않아도 된다."""
  # 첫 묶음(= 아직 안 누른 조각들)만 본다. 이미 문장에 놓인 낱말 묶음까지
  # 같이 보면, 'My' 하나로 여러 문장이 후보가 되면서 판단을 망친다.
  usable = _usable_items(groups[0]) if groups else []
  if usable:
    chips = [_norm_token(t) for _, t in usable]
    best = None
    best_tie = False

    for word in word_list:
      eng = word.get('eng', '')
      # '끊어읽기' 세트는 한 카드의 문장이 ' / '로 토막 나 있고, 사이트는
      # 토막을 하나씩 따로 물어본다. 문장 전체로만 맞춰보면 조각이 모자라서
      # 엉뚱하게 '문장 전체'를 누르려다 실패했다(한 토막마다 3초씩 허비).
      # 그래서 문장 전체와 토막 하나하나를 모두 후보로 놓고 맞춰본다.
      for cand in _sentence_chunks(eng):
        tokens = _sentence_tokens(cand)
        if not tokens or len(chips) > len(tokens):
          continue
        if not _is_submultiset(chips, tokens):
          continue

        extra = len(tokens) - len(chips)
        if best is None or extra < best[0]:
          best = (extra, word, tokens, cand)
          best_tie = False
        elif extra == best[0]:
          # 후보가 둘이어도 '눌러야 할 낱말 순서'가 똑같으면 어느 쪽이든
          # 상관없다. 예전에는 카드 이름이 다르기만 하면 무조건 포기했는데,
          # 한 세트에 'Now I understand.'와 'Now I understand / a lot of
          # them.'이 같이 들어 있으면(끊어읽기 세트에서 흔하다) 조각이
          # ['Now','I','understand']로 똑같이 나와서 영영 안 풀었다.
          if tokens != best[2]:
            best_tie = True
          elif (len(_sentence_chunks(word.get('eng', '') or ''))
                > len(_sentence_chunks(best[1].get('eng', '') or ''))):
            # 순서가 같다면 토막이 더 많은 카드로 잡아둔다. '아직 토막이
            # 남았다'고 보면 제출을 안 하는데, 실제로 한 문장짜리였더라도
            # 다음 화면(Good Job!/영작 연습하기)에서 알아서 넘어간다.
            # 반대로 잘못 제출하면 토막이 남은 카드를 통째로 건너뛴다.
            best = (extra, word, tokens, cand)

    if best is None:
      return None
    if best_tie:
      print(f'[DEBUG] 조각만으로 문장을 특정할 수 없어 건너뜀 (남는 낱말 {best[0]}개)')
      return None

    _, word, tokens, source = best
    return usable, word, tokens, source

  return None


def _pick_group_for(groups, tokens):
  """조각 묶음 중 '아직 안 누른 조각들'이 담긴 묶음을 고른다.

  read_scramble_groups가 이미 누를 수 있는 묶음만, 화면 아래쪽부터 정렬해
  돌려주므로, 문장 낱말과 겹치는 게 하나라도 있는 첫 묶음을 쓴다. 겹치는
  개수가 가장 많은 것을 고르면 안 된다 - 이미 문장에 놓인 낱말이 더 많이
  쌓여 있으면 그쪽이 뽑혀서 같은 낱말을 또 누르게 된다."""
  wanted = set(tokens)
  for items in groups:
    usable = _usable_items(items)
    if any(_norm_token(txt) in wanted for _, txt in usable):
      return usable
  return []


_PLACED_TEXT_JS = r"""
// 화면에 보이는 요소들의 글자를 모아 돌려준다. 문장 줄에 이미 채워진
// 앞부분(리콜은 앞 낱말 몇 개를 미리 놓아준다)을 알아내기 위한 것이다.
// 너무 긴 글자는 문단이나 페이지 전체이므로 제외한다.
var CHIP = '.scramble-item, .btn-scramble, [class*="c2NyYW1i"]';

function isUsedChip(c) {
  if (c.classList.contains('clicked')) return true;
  try {
    return /,\s*0\s*\)$/.test(getComputedStyle(c).color);
  } catch (e) {
    return false;
  }
}

var out = [];
var nodes = document.querySelectorAll('div, span, p, td, li, h1, h2, h3, label');
for (var i = 0; i < nodes.length && out.length < 300; i++) {
  var el = nodes[i];
  // 조각 자체는 세면 안 된다. 조각 'I' 하나가 문장 첫 낱말과 같아서
  // '앞 1낱말이 이미 놓였다'고 잘못 읽는 일이 있었다.
  if (el.closest && el.closest(CHIP)) continue;
  // 아직 안 누른 조각을 품고 있는 상자도 마찬가지다. 조각 하나를 감싼
  // 바깥 상자의 글자가 그 조각과 같아서 똑같이 속았다. 이미 놓인 낱말만
  // 담고 있는 상자(문장 줄)는 그대로 센다.
  var inner = el.querySelectorAll(CHIP);
  var hasUnused = false;
  for (var j = 0; j < inner.length; j++) {
    if (!isUsedChip(inner[j])) { hasUnused = true; break; }
  }
  if (hasUnused) continue;
  var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
  if (!t || t.length > 300) continue;
  if (typeof el.checkVisibility === 'function') {
    try {
      if (!(function (e) {
      // 창을 내려놓거나 다른 탭을 보고 있으면(document.hidden) 크롬의
      // checkVisibility가 멀쩡히 그려진 것도 전부 false로 답한다.
      // 그럴 때는 크기와 조상 스타일로 직접 판단한다.
      if (!document.hidden) {
        try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); }
        catch (err) {}
      }
      var r = e.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return false;
      var n = e;
      while (n && n.nodeType === 1) {
        var cs = getComputedStyle(n);
        if (cs.display === "none" || cs.visibility === "hidden" ||
            parseFloat(cs.opacity) === 0) return false;
        n = n.parentElement;
      }
      return true;
    })(el)) {
        continue;
      }
    } catch (e) {}
  }
  var r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) continue;
  out.push(t);
}

// 리콜의 문장 줄(.input-box)은 낱말이 <div>로 하나씩 들어 있어서
// textContent가 'Iusedthe'처럼 붙어버리고, 빈칸 표시(class 'now')가
// '아직 안 누른 조각'으로 잡혀 줄 전체가 통째로 걸러졌다. 그래서 'I used
// the'가 놓여 있어도 1낱말로만 읽혀서 시작 자리를 잘못 잡았다.
// 자식 글자를 공백으로 이어 붙여 따로 넣는다.
var boxes = document.querySelectorAll('.input-box');
for (var b = 0; b < boxes.length; b++) {
  var box = boxes[b];
  var br = box.getBoundingClientRect();
  if (br.width <= 0 || br.height <= 0) continue;
  var words = [];
  for (var c = 0; c < box.children.length; c++) {
    var ch = box.children[c];
    if (ch.classList && ch.classList.contains('now')) continue;  // 빈칸 표시
    var ct = (ch.textContent || '').replace(/\s+/g, ' ').trim();
    if (!ct || /^[0-9]+\.$/.test(ct)) continue;  // 앞의 번호('1.')
    words.push(ct);
  }
  if (words.length) out.push(words.join(' '));
}
return out;
"""


def read_placed_count(driver, tokens, limit):
  """문장 줄에 이미 채워진 낱말이 몇 개인지 화면 글자로 알아낸다.

  조각 트레이는 문장 순서대로 일정 개수씩 밀려 나오는 창이라, 같은 낱말이
  반복되는 문장에서는 조각만으로 창의 시작 위치를 정할 수 없다('I ... I ...'
  는 0번째부터 봐도, 1번째부터 봐도 구성이 같다). 화면에 이미 놓인 앞부분을
  읽으면 그 자리가 확정된다.

  limit(= 전체 낱말 수 - 남은 조각 수)보다 큰 건 무시한다. 오답 뒤에 뜨는
  '정답' 줄처럼 문장 전체가 적힌 글자에 속지 않기 위해서다."""
  if limit <= 0:
    return 0

  try:
    texts = driver.execute_script(_PLACED_TEXT_JS) or []
  except Exception:
    return 0

  best = 0
  for text in texts:
    words = [w for w in (_norm_token(x) for x in _norm_quotes(text).split()) if w]
    n = len(words)
    if n <= best or n > limit:
      continue
    if words == tokens[:n]:
      best = n
  return best


def _next_token_index(tokens, chips, placed=None):
  """남은 조각들로 미루어, 지금 눌러야 할 낱말이 문장의 몇 번째인지.

  조각 트레이는 문장 순서대로 일정 개수씩 밀려 나오는 '창'이다. 긴 문장은
  앞에서부터 7개만 DOM에 있고(F12로 확인), 하나 누를 때마다 뒤가 채워진다.
  그래서 조각 구성은 언제나 tokens의 연속된 한 구간과 정확히 일치한다.
  그 구간이 시작되는 자리가 곧 지금 눌러야 할 자리다.

  같은 낱말이 반복되는 문장이면 맞는 구간이 여럿일 수 있다. 그때는 화면에
  이미 놓인 앞부분 낱말 수(placed)와 맞는 것을 고르고, 그래도 못 정하면
  앞쪽을 쓴다."""
  n = len(chips)
  if n == 0 or n > len(tokens):
    return None

  wanted = sorted(chips)
  found = [
      k for k in range(len(tokens) - n + 1)
      if sorted(tokens[k:k + n]) == wanted
  ]
  if not found:
    return None
  if len(found) == 1 or placed is None:
    return found[0]
  if placed in found:
    return placed
  # 어디서부터인지 확신할 수 없으면 건드리지 않는다. 찍어서 누르면 틀린
  # 답이 그대로 제출된다.
  return None


# 화면(주소)별로 실제로 먹힌 조각 클릭 방식. True=셀레니움, False=JS.
# 문장 리콜은 JS 클릭이 안 먹고 셀레니움 클릭만 먹어서, 조각마다 JS로 한 번
# 헛클릭 -> 확인 -> 셀레니움으로 다시 누르느라 느렸다(로그: 매 조각 '클릭이
# 안 먹은 듯해서 다시 시도'). 한 번 먹힌 방식을 기억해 다음부터 바로 쓴다.
_chip_click_native = {}


def _study_page_kind(driver):
  try:
    url = (driver.current_url or '').lower()
  except Exception:
    return ''
  for hint in ('/memorize/', '/recall/', '/spell/', '/classtest/'):
    if hint in url:
      return hint
  return ''


def _case_token(text):
  """_norm_token과 같지만 대소문자를 그대로 둔다.

  'The plants clean the water by consuming the food.'처럼 같은 낱말이
  대소문자만 다르게 여러 번 나오는 문장이 있다. 낱말 비교는 소문자로
  하지만, 누를 조각을 고를 때는 대소문자까지 같은 조각을 먼저 골라야 한다.
  사이트는 조각 글자를 그대로 비교해서, 'The' 자리에 'the'를 누르면
  오답 처리한다(로그: 'the' 클릭이 안 먹은 듯 -> 그 문장 전부 오답)."""
  t = _norm_quotes(text)
  cleaned = re.sub(r"[^0-9A-Za-zÀ-ɏ]+", '', t)
  return cleaned or t.strip()


def _chip_match_rank(chip_text, raw_word):
  """조각 글자가 원래 낱말과 얼마나 똑같은지. 작을수록 우선.

  0 = 문장부호까지 완전히 같음, 1 = 대소문자까지 같음(부호만 다름),
  2 = 소문자로는 같음, 3 = 그 밖에."""
  if not raw_word:
    return 2
  a = _norm_quotes(chip_text).strip()
  b = _norm_quotes(raw_word).strip()
  if a == b:
    return 0
  if _case_token(a) == _case_token(b):
    return 1
  if _norm_token(a) == _norm_token(b):
    return 2
  return 3


def click_scramble_in_order(driver, tokens, raw_tokens=None):
  """정답 순서대로 조각을 클릭한다.

  시작 자리는 처음 읽은 조각으로 한 번만 정하고, 그 뒤로는 우리가 누른
  횟수로 진행 상황을 안다. 조각이 다 나와 있지 않아서(창 방식) 매번 다시
  계산하면 자리를 잘못 잡는다.

  누른 조각은 문장 줄로 옮겨가면서 같은 클래스를 그대로 달고 있으므로,
  이미 누른 것은 따로 기억해 다시 누르지 않는다. 처음 정한 자리부터 문장
  끝까지 다 누르면 끝낸다."""
  start = None
  needed = 0
  done_count = 0
  used = []
  retries = {}
  # 테스트 화면 조각은 '진짜 클릭'만 받는다. 사이트 코드
  # (class_test_sentence.js)를 풀어보니 클릭 이벤트의 isTrusted가 false면
  # 그냥 무시한다 - 자바스크립트로 만든 클릭은 전부 헛클릭이다. 그래서
  # 테스트에서는 셀레니움 클릭만 쓴다.
  page = _study_page_kind(driver)
  in_test = page == '/classtest/'
  native_click = True if in_test else _chip_click_native.get(page, False)
  # 조각 줄이 새로 그려질 수 있는 때(카드 시작, 줄을 다 누른 뒤, 클릭 실패
  # 뒤)에만 '줄이 멈췄는지' 확인한다. 매번 하면 조각마다 느려진다.
  need_stable = True
  t_begin = time.time()
  landed_waits = []

  def report():
    # 조각을 누르는 속도가 느리다고 해서, 어디서 시간이 드는지 로그로 본다.
    if done_count:
      took = time.time() - t_begin
      avg_wait = sum(landed_waits) / len(landed_waits) if landed_waits else 0
      print(f'[DEBUG] 조각 {done_count}개 {took:.2f}초 (조각당 {took / done_count:.2f}초,'
            f' 눌린 것 확인 평균 {avg_wait:.2f}초)')

  for step in range(len(tokens) * 3 + 6):
    if not is_running:
      return False

    items = []
    for attempt in range(30):  # 다음 조각이 나타날 때까지 최대 1.5초
      if not is_running:
        return False
      groups = read_scramble_groups(driver)
      items = [
          (el, txt)
          for el, txt in _pick_group_for(groups, tokens)
          if not any(el == prev for prev in used)
      ]
      if items and in_test and need_stable:
        # 테스트는 카드가 넘어오는 순간 조각이 알파벳 순서로 잠깐 보였다가
        # 섞인 순서로 새로 그려진다. 셀레니움 클릭은 좌표로 누르므로, 다시
        # 그려지기 전 조각을 보고 누르면 그 자리에 새로 온 엉뚱한 조각이
        # 눌렸다(영상: Traditional 대신 Korean). 잠깐 뒤 다시 읽어서 조각
        # 줄이 그대로일 때만 누른다.
        time.sleep(0.12)
        again = [
            (el, txt)
            for el, txt in _pick_group_for(read_scramble_groups(driver), tokens)
            if not any(el == prev for prev in used)
        ]
        if [e.id for e, _ in again] != [e.id for e, _ in items]:
          items = []
          continue
        items = again
        need_stable = False
      if items:
        break
      # 조각을 누르면 트레이가 다시 그려지느라 잠깐 비는 순간이 있다.
      time.sleep(0.05)

    if not items:
      report()
      return start is not None and done_count >= needed

    if start is None:
      chips = [_norm_token(txt) for _, txt in items]
      placed = read_placed_count(driver, tokens, len(tokens) - len(chips))
      start = _next_token_index(tokens, chips, placed)
      if start is None:
        print(
            f'[DEBUG] 조각 {len(chips)}개가 문장의 어느 구간과도 안 맞아 중단'
            f' (조각={chips})'
        )
        return False
      needed = len(tokens) - start
      if start:
        print(f'[DEBUG] 앞 {start}낱말은 이미 놓여 있어 그다음부터 누른다')

    idx = start + done_count
    if idx >= len(tokens):
      report()
      return True

    tok = tokens[idx]
    raw_tok = raw_tokens[idx] if raw_tokens and idx < len(raw_tokens) else None
    # 같은 낱말이 여러 개일 때 어느 조각을 누를지 고른다. 'This process is
    # repeated again and again!'처럼 문장부호나 대소문자만 다른 조각이
    # 같이 있는 경우가 있어서(again / again!), 원래 낱말과 가장 똑같은
    # 조각부터 고른다. 순서를 어기면 사이트가 바로 오답 처리한다.
    cands = [(el, txt) for el, txt in items if _norm_token(txt) == tok]
    target = None
    if cands:
      target = min(
          cands, key=lambda it: (_chip_match_rank(it[1], raw_tok), cands.index(it))
      )[0]

    if target is None:
      # 눌러야 할 낱말이 아직 트레이에 안 들어왔다. 잠깐 뒤에 다시 본다.
      time.sleep(0.05)
      continue

    if in_test:
      # 이미 채점된 카드(정답/오답)면 더 누르지 않는다. 그 카드 조각은
      # 클릭을 안 받는다(pointer-events: none).
      try:
        judged = driver.execute_script(
            "var c = arguments[0].closest('.flip-card');"
            "if (!c) return '';"
            "return c.classList.contains('correct') ? 'correct'"
            " : (c.classList.contains('wrong') ? 'wrong' : '');", target)
      except Exception:
        judged = ''
      if judged:
        print(f'[DEBUG] 이 카드는 이미 채점됨({judged}) - 조각 누르기 중단')
        report()
        return judged == 'correct'

    # 한 번 실패한 낱말은 반대 방식으로 바꿔 본다. (테스트는 JS 클릭이
    # 무시되므로 바꾸지 않는다)
    flip = retries.get(tok, 0) > 0 and not in_test
    if not click_chip(
        driver, target, native_first=native_click != flip, debug=flip,
        allow_js=not in_test,
    ):
      need_stable = True
      time.sleep(0.1)
      continue

    used.append(target)
    done_count += 1

    # 클릭이 실제로 먹었는지 확인한다. 두 가지 중 하나면 성공으로 본다.
    #  1) 그 조각이 남은 조각 목록에서 빠졌다(다 쓴 조각으로 바뀌었다)
    #  2) 문장 줄에 그 낱말까지 채워졌다
    # 화면마다 다 쓴 조각을 표시하는 방식이 달라서 둘 다 본다.
    landed = False
    t_click = time.time()
    # 누른 직후 바로 확인하면 트레이가 다시 그려지는 중이라 '빠졌다'로
    # 잘못 보고, 다음 조각을 너무 빨리 눌러 순서가 꼬였다(조각당 0.03초,
    # 전부 오답). 예전처럼 조금 기다린 뒤 확인한다.
    for _ in range(12):
      time.sleep(0.03)
      # 1) 누른 조각에 'clicked'가 붙었으면 확실히 눌린 것이다. 테스트에서
      #    조각 줄의 마지막 조각을 누르면 남은 조각이 없어져 '묶음을 못
      #    찾음'이 되는 바람에 눌린 걸 확인 못 하고 같은 조각을 다시 눌렀다.
      #    테스트는 이미 누른 조각을 또 누르면 오답(시간 차감)이다.
      try:
        state = driver.execute_script(
            "var el = arguments[0];"
            "if (el.classList.contains('clicked')) return 'clicked';"
            "if (el.classList.contains('shake') || el.classList.contains('wrong'))"
            "  return 'wrong';"
            "var p = el.parentElement;"
            "return p && p.classList.contains('disabled') ? 'wrong' : '';", target)
        if state == 'clicked':
          landed = True
          break
        if state == 'wrong':
          # 사이트가 이 조각을 오답으로 처리했다(shake + 조각 줄 disabled).
          # 예전엔 조각 줄이 다시 그려진 걸 '눌렸다'로 착각해서 클릭 방식을
          # 잘못 바꾸고(0.08초 -> 0.44초) 그대로 진행했다.
          print(f"[DEBUG] '{tok}'이(가) 오답 처리됨 - 이번 문장 중단")
          report()
          return False
      except StaleElementReferenceException:
        # 눌린 뒤 조각 줄이 새로 그려져 요소가 사라졌다 = 눌린 것.
        landed = True
        break
      except Exception:
        pass
      group = _pick_group_for(read_scramble_groups(driver), tokens)
      # 묶음을 아예 못 찾은 상태를 '빠졌다'로 보면 안 된다. 엉뚱한 요소를
      # 조각으로 잡던 시절, 이것 때문에 안 눌린 것을 눌린 걸로 착각했다.
      if group and not any(el == target for el, _ in group):
        landed = True
        break
      if read_placed_count(driver, tokens, len(tokens)) >= idx + 1:
        landed = True
        break

    landed_waits.append(time.time() - t_click)
    if len(items) <= 1 or not landed:
      # 줄의 마지막 조각이었거나 실패했다 - 다음 줄이 새로 그려질 수 있다.
      need_stable = True
    if landed and flip and page != '/classtest/':
      # (테스트는 아직 확인 전이라 원래 동작 그대로 둔다)
      # 바꿔 본 방식이 먹혔다. 이 화면에서는 앞으로 그 방식을 먼저 쓴다.
      native_click = not native_click
      _chip_click_native[page] = native_click
      retries.clear()
      print(f"[DEBUG] 이 화면은 {'셀레니움' if native_click else 'JS'} 클릭이 먹힘 - 앞으로 이걸 먼저 씀")
    if not landed:
      retries[tok] = retries.get(tok, 0) + 1
      if retries[tok] > 3:
        print(f"[DEBUG] '{tok}'을 계속 못 눌러서 이번 문장 포기")
        return False
      print(f"[DEBUG] '{tok}' 클릭이 안 먹은 듯해서 다시 시도")
      used.pop()
      done_count -= 1
    elif done_count >= needed:
      report()
      return True

    time.sleep(0.04)

  print('[DEBUG] 조각을 다 못 눌러서 중단')
  return False


def _press_next_question_when_ready(driver, timeout=2.0):
  """테스트의 [다음 문제] 버튼이 뜨면 바로 눌러 넘긴다. 눌렀으면 True.

  timeout 동안 0.05초 간격으로 기다린다(0이면 한 번만 본다). 클릭이 안
  먹으면 ENTER(버튼에 붙은 단축키)를 보낸다."""
  end = time.time() + timeout
  while True:
    btn = find_button_by_text(driver, _NEXT_CARD_LABELS)
    if btn:
      print('[DEBUG] 다음 문제 버튼 감지 - 바로 누름')
      try:
        btn.click()
      except Exception:
        pass
      time.sleep(0.12)
      if find_button_by_text(driver, _NEXT_CARD_LABELS):
        press_submit(driver)
        time.sleep(0.1)
      return True
    if not is_running or time.time() >= end:
      return False
    time.sleep(0.05)


_UNARRANGED_WARNING_JS = r"""
// '아직 배열하지 않은 단어가 있습니다' 확인창이 화면에 떠 있는지.
var nodes = document.querySelectorAll('body *');
for (var i = 0; i < nodes.length; i++) {
  var el = nodes[i];
  if (el.children.length) continue;
  if ((el.textContent || '').indexOf('배열하지 않은') === -1) continue;
  var r = el.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) return true;
}
return false;
"""


def _unarranged_warning_visible(driver):
  try:
    return bool(driver.execute_script(_UNARRANGED_WARNING_JS))
  except Exception:
    return False


# --- 문장 암기(영작 연습) 자동 풀이 스레드 ---
def sentence_scramble_worker(mode_name='문장'):
  global is_running, shared_driver
  is_running = True

  btn_load.config(state=tk.DISABLED)
  btn_start.config(state=tk.DISABLED)
  btn_recall_start.config(state=tk.DISABLED)
  btn_spell_start.config(state=tk.DISABLED)
  btn_test_start.config(state=tk.DISABLED)
  btn_stop.config(state=tk.NORMAL)

  try:
    driver = get_driver()
    # 시작하자마자, 화면에 열린 세트와 불러온 단어가 같은지 맞춰본다.
    ensure_word_list_for_screen(driver, min_gap=0)
    root.after(
        0,
        lambda: lbl_status.config(
            text=f'브라우저 준비 중... {mode_name} 학습 화면으로 이동하세요',
            fg='#0288D1',
        ),
    )

    solved_key = None
    stuck = 0
    open_logged = False
    open_tries = 0

    while is_running:
      # 덜 배열한 채 제출하면 확인창이 뜨고, 그게 떠 있는 동안은 조각을
      # 눌러도 아무 반응이 없다. 보이면 [취소]로 닫고 다시 시작한다.
      # 이 확인창은 테스트 화면에만 있다 - 다른 화면에서까지 '취소'를
      # 찾으면 엉뚱한 버튼을 누를 수 있으므로 주소로 제한한다.
      # 단, 테스트 시작 화면의 '응시하시겠습니까?/새로 시작할까요?' 창에도
      # [취소]가 있어서, 사용자가 테스트를 시작하려 할 때마다 매크로가
      # [취소]를 눌러 창을 닫아버렸다. 그 확인창 문구가 보일 때만 누른다.
      if (on_class_test(driver) and _unarranged_warning_visible(driver)
          and click_button_by_text(driver, _CANCEL_LABELS, wait_after=0.4)):
        solved_key = None
        continue

      # 세트/구간 완료 화면('리콜 200% 완료' + [300% 도전], [다음 구간으로
      # 이동])이면 먼저 넘긴다. 예전엔 조각이 안 보일 때만 이걸 봤는데, 완료
      # 화면 뒤에 지난 카드 조각이 남아 있으면 그걸 문제로 착각해서 완료
      # 버튼을 끝내 안 눌렀다.
      if handle_section_done(driver):
        solved_key = None
        continue

      # 테스트는 채점이 끝나면 'Good Job!'/오답 표시와 [다음 문제](ENTER)가
      # 뜬다. 그 카드 조각은 화면에 그대로 남아 있어서, 조각부터 보면 다시
      # 풀려고 헛돈다. 다음 문제 버튼을 먼저 본다.
      if on_class_test(driver) and _press_next_question_when_ready(driver, 0):
        solved_key = None
        continue

      groups = read_scramble_groups(driver)
      matched = match_scramble_sentence(groups)

      if not matched:
        # 조각이 없는 화면: [영작 연습하기]로 문제를 열거나, 구간이 끝났으면
        # 다음 구간으로 넘어간다.
        # [영작 연습하기] 화면. 셀레니움 클릭은 먹히지 않아서(눌러도 화면이
        # 그대로였다) 화면 안내대로 스페이스를 보낸다.
        if find_button_by_text(driver, _WRITE_PRACTICE_LABELS):
          if not open_logged:
            print('[DEBUG] 영작 연습하기 화면 - 스페이스로 문제 열기')
            open_logged = True
          open_tries += 1
          if open_tries == 8 and not warn_if_no_cards(driver):
            # 주소를 직접 치거나 새로고침해서 들어오면, 사이트가 카드를
            # 하나도 안 내주는 때가 있다(화면 위 숫자가 '0/12'가 아니라
            # '0/0'이고, 지난번에 하던 다른 세트의 문장이 남아 있다).
            # 그러면 [영작 연습하기]를 아무리 눌러도 화면이 안 넘어간다.
            # 예전에는 여기서 아무 말 없이 계속 누르기만 했다.
            print('[DEBUG] [영작 연습하기]를 여러 번 눌러도 화면이 안 넘어감')
            root.after(0, lambda: lbl_status.config(
                text='화면이 안 넘어갑니다. 세트 화면에서 학습 버튼을 눌러\n'
                     '다시 들어와 주세요. (주소를 직접 열면 안 될 때가 있어요)',
                fg='#D32F2F'))
          press_space(driver)
          time.sleep(0.25)  # 사용자 요청으로 점점 빠르게 (0.55 -> 0.4 -> 0.25)
          # 자동으로 주소를 열고 들어오면 이 버튼에 스페이스 단축키
          # (data-hotkey)가 안 붙어 있는 경우가 있다. 그때는 스페이스를
          # 아무리 보내도 화면이 안 넘어가서, 버튼을 직접 누른다.
          if find_button_by_text(driver, _WRITE_PRACTICE_LABELS):
            print('[DEBUG] 스페이스가 안 먹어서 [영작 연습하기]를 직접 누름')
            click_button_by_text(driver, _WRITE_PRACTICE_LABELS, wait_after=0.6)
          continue
        open_logged = False
        if handle_section_done(driver):
          solved_key = None
          continue
        # 'Good Job!' + [한번 더]/[다음 카드]만 있는 화면. 스페이스가 안 먹는다.
        if click_button_by_text(driver, _NEXT_CARD_LABELS, wait_after=0.2):
          solved_key = None
          continue
        if groups:
          print(f"[DEBUG] 조각은 보이는데 맞는 문장이 없음={[[t for _, t in g] for g in groups]}")
        focus_study_tab(driver)
        # 화면에 열린 세트와 불러온 문장이 다르면 여기서 맞춘다.
        if ensure_word_list_for_screen(driver):
          continue
        root.after(
            0,
            lambda: lbl_status.config(
                text=f'{mode_name} 학습 화면을 기다리는 중...', fg='gray'
            ),
        )
        time.sleep(0.3)
        continue

      items, word, tokens, source = matched
      open_tries = 0
      key = tuple(sorted(_norm_token(t) for _, t in items))

      if key == solved_key:
        # 방금 푼 화면이 아직 안 넘어갔다. 조각을 또 누르면 답이 망가지므로
        # 스페이스만 다시 보낸다. 그래도 안 넘어가면 같은 문장이 또 나온
        # 것으로 보고 다시 푼다.
        #
        # 예전에는 0.5초씩 통째로 쉬면서 여섯 번까지 기다렸다. 그래서 문장을
        # 하나 풀 때마다 3초씩 노는 것처럼 보였다. 이제는 0.12초마다 화면을
        # 들여다보고(다음 문장이 뜨는 즉시 바로 시작), 스페이스는 예전처럼
        # 0.5초에 한 번씩만 보낸다(너무 자주 보내면 문장을 건너뛸 수 있다).
        stuck += 1
        if stuck > 14:   # 0.12초씩 -> 1.7초쯤 보고 포기한다(예전 2.9초)
          print('[DEBUG] 같은 문장이 계속 떠서 다시 푼다')
          solved_key = None
          stuck = 0
        else:
          if stuck % 2 == 1:
            press_space(driver)
          time.sleep(0.12)
        continue

      print(
          f"[DEBUG] 조각들={[t for _, t in items]} -> 맞출 부분='{source.strip()}'"
      )
      msg = f"문장 감지: {word['eng']}"
      root.after(0, lambda m=msg: lbl_status.config(text=m, fg='#0288D1'))

      # 문장부호까지 그대로 둔 원래 낱말들(조각 고를 때 기준).
      # 끊어읽기 세트면 맞춘 '토막'만 쓴다(문장 전체가 아니라).
      raw_tokens = [
          w for w in _norm_quotes(source).split() if _norm_token(w)
      ]
      if len(raw_tokens) != len(tokens):
        raw_tokens = None
      # 이 카드에 아직 풀 토막이 남았는지 본다.
      #
      # '끊어읽기' 세트는 카드 하나가 ' / '로 토막 나 있고, 한 토막을 맞히면
      # 사이트가 같은 카드의 다음 토막을 이어서 보여준다. 그런데 매크로가
      # 토막마다 ENTER(제출)를 눌러서, 카드를 다 못 끝내고 다음 카드로
      # 넘어가 버렸다. 그래서 문장 암기 진도가 50%에서 더 안 올랐다
      # (한 토막짜리 카드만 제대로 끝났다).
      chunks = _sentence_chunks(word.get('eng', '') or '')
      parts = chunks[1:] if len(chunks) > 1 else chunks
      more_chunks = (len(parts) > 1
                     and source.strip() != chunks[0].strip()
                     and source.strip() != parts[-1].strip())

      done = click_scramble_in_order(driver, tokens, raw_tokens)
      print(f'[DEBUG] 배열성공={done}')
      if done:
        # 토막을 한꺼번에 물어본 화면이면 그 카드의 토막을 모두 센다.
        if len(chunks) > 1 and source.strip() == chunks[0].strip():
          for part in parts:
            _auto_card_done(part)
        else:
          _auto_card_done(source)

      if done and is_running:
        solved_key = key
        stuck = 0
        root.after(
            0,
            lambda t=word['eng']: lbl_status.config(
                text=f"'{t}' 완성! 다음 문장 이동", fg='#388E3C'
            ),
        )
        # 사용자 요청으로 살짝 빠르게 (0.2/0.45 -> 0.1/0.35). 테스트는 아직
        # 실제로 확인을 안 해서 원래 값 그대로 둔다.
        in_test = on_class_test(driver)
        if more_chunks and not in_test:
          # 같은 카드의 다음 토막이 이어서 나온다. 여기서 제출하면 카드를
          # 통째로 넘겨버리므로 아무것도 누르지 않고 다음 토막을 푼다.
          time.sleep(0.15)
        elif in_test:
          # 테스트는 다 맞추면 사이트가 알아서 채점하고 [다음 문제](ENTER)를
          # 띄운다. 예전엔 ENTER 한 번 + 0.45초 쉬고 다음 반복에서 버튼을
          # 찾느라 살짝 늦었다. 버튼이 뜨는 순간을 0.05초 간격으로 지켜보다가
          # 바로 누른다(최대 2초, 안 뜨면 예전처럼 ENTER).
          # 먼저 ENTER(제출)를 보내 바로 채점되게 하고(사이트가 스스로
          # 채점하려면 0.8초를 기다린다), 곧이어 뜨는 [다음 문제]를 누른다.
          press_submit(driver)
          _press_next_question_when_ready(driver, timeout=2.0)
        else:
          press_submit(driver)
          # 다음 화면('문장 확인' 또는 바로 다음 문제)이 나오는 즉시 움직인다.
          _wait_next_sentence(driver, key)
      else:
        time.sleep(0.5)

  except Exception as e:
    err_msg = str(e)
    root.after(
        0,
        lambda: messagebox.showerror(
            '셀레니움 에러', f'오류가 발생했습니다:\n{err_msg}'
        ),
    )
  finally:
    root.after(0, stop_macro)


# --- 완전 자동 학습 ---
#
# 클래스 -> 세트 -> 목표(%)를 고르면, 세트마다 단어/문장을 사이트에서 직접
# 읽어와 암기 -> 리콜 -> 스펠 (-> 테스트) 순서로 알아서 돌린다.
auto_running = False
auto_mode_done = threading.Event()

# 이번 모드에서 카드를 몇 장 했는지 센다.
#
# 사이트가 '세트를 다 끝냈다'는 걸 모드마다 다르게 보여준다. 암기/리콜은
# 멈출 수 있는 화면이 뜨는데 스펠은 안 떠서, 스펠이 100%에서 안 멈추고
# 340%까지 같은 단어를 계속 돌았다. 그래서 화면 대신 '서로 다른 카드를
# 몇 장 했는지'를 센다. 세트의 카드를 전부 한 번씩 했으면 한 바퀴다.
auto_cards_done = 0
auto_cards_seen = set()

# 지금 무엇을 하고 있는지 적어두는 값들.
# 예전에는 이걸로 세트 줄에 '▶ 스펠 12/25' 같은 실시간 진도를 보여줬는데,
# 숫자가 실제와 안 맞을 때가 많아서(카드를 다시 보여주거나 토막으로 쪼개는
# 세트가 있다) 표시를 없앴다. auto_cards_total은 '한 바퀴 다 돌았나'를
# 판단하는 데 계속 쓴다.
auto_cur_idx = None      # 지금 학습 중인 세트 번호
auto_cur_mode = None     # 'memorize' / 'recall' / 'spell' / 'test'
auto_cards_total = 0     # 이 세트의 카드 수


_PROGRESS_JS = r'''
// 학습 화면 위쪽의 진행 표시('6/12')를 읽는다. 사이트가 세는 방식이
// 가장 정확하다(끊어읽기 세트는 토막이 여러 개라도 카드 하나로 센다).
var t = (document.body.innerText || '').split('\n');
for (var i = 0; i < t.length; i++) {
  var m = t[i].trim().match(/^([0-9]+)\s*\/\s*([0-9]+)$/);
  if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];
}
return null;
'''

# 사이트가 말하는 '몇 장 중 몇 장'. 워커 갈래에서만 채운다.
auto_site_done = 0
auto_site_total = 0


def _read_study_progress():
  """학습 화면의 진행 표시를 읽어 적어둔다. (워커 갈래에서만 부를 것)"""
  global auto_site_done, auto_site_total
  driver = shared_driver
  if driver is None:
    return
  try:
    out = driver.execute_script(_PROGRESS_JS)
  except Exception:
    return
  if out and len(out) == 2:
    before = (auto_site_done, auto_site_total)
    auto_site_done, auto_site_total = int(out[0]), int(out[1])
    if (auto_site_done, auto_site_total) != before:
      print(f'[DEBUG] 사이트 진행 {auto_site_done}/{auto_site_total}')


def _study_unit_count(cards):
  """한 바퀴에 몇 장을 풀어야 하는지.

  '끊어읽기' 문장 세트는 카드 하나가 ' / '로 토막 나 있고 사이트는 토막마다
  한 문제씩 낸다. 카드 수로만 세면 절반만 하고 '한 바퀴 끝'이라고 착각한다
  (12카드 세트를 12장 풀고 끝냈는데 진도는 50%였다)."""
  total = 0
  for card in cards or []:
    parts = _sentence_chunks(card.get('eng', '') or '')
    total += len(parts) - 1 if len(parts) > 1 else 1
  return total


def _auto_card_done(word=None):
  """워커가 카드 한 장(문장 하나)을 끝낼 때마다 부른다."""
  global auto_cards_done
  if not auto_running:
    return
  auto_cards_done += 1
  if isinstance(word, dict):
    word = word.get('eng')
  if word:
    auto_cards_seen.add(str(word).strip().lower())
  _read_study_progress()


_MODE_PATH = {'memorize': 'Memorize', 'recall': 'Recall', 'spell': 'Spell'}
_MODE_NAME = {'memorize': '암기', 'recall': '리콜', 'spell': '스펠'}


def _auto_status(text, color='#0288D1'):
  def work():
    for label in ('lbl_auto_status', 'lbl_status'):
      widget = globals().get(label)
      if widget is not None:
        try:
          widget.config(text=text, fg=color)
        except Exception:
          pass
  root.after(0, work)


def _auto_log(text):
  print('[AUTO] ' + text)
  _auto_status(text)


def _auto_run_worker(target, cards=0, timeout=1800):
  """워커를 돌리고 이 모드를 한 바퀴 돌 때까지 기다린다.

  끝난 걸 두 가지로 알아챈다.
  1) 세트 완료 화면(handle_section_done이 auto_mode_done을 세운다)
  2) 카드를 세트 장수만큼 처리했을 때 — 완료 화면의 버튼 이름이 모드마다
     달라서 1)을 놓치는 일이 있었다(스펠이 100%에서 안 멈추고 340%까지
     돌았다). 한 바퀴어치를 다 했으면 화면이 뭐라고 하든 여기서 끊는다."""
  global is_running, auto_cards_done
  global auto_site_done, auto_site_total
  auto_mode_done.clear()
  auto_cards_done = 0
  auto_cards_seen.clear()
  auto_site_done = auto_site_total = 0
  t = threading.Thread(target=target, daemon=True)
  t.start()
  end = time.time() + timeout
  while t.is_alive() and auto_running and time.time() < end:
    if auto_mode_done.is_set():
      break
    # 사이트가 '12/12'처럼 다 했다고 하면 그게 가장 정확하다.
    if auto_site_total and auto_site_done >= auto_site_total:
      print(f'[AUTO] 사이트 진행 {auto_site_done}/{auto_site_total} - 한 바퀴 끝')
      auto_mode_done.set()
      break
    # 서로 다른 카드를 세트 장수만큼 했으면 한 바퀴를 다 돈 것이다.
    if cards and len(auto_cards_seen) >= cards:
      print(f'[AUTO] 카드 {cards}장을 한 번씩 다 해서 한 바퀴 끝')
      auto_mode_done.set()
      break
    # 카드 이름을 못 세는 모드(암기)를 위한 안전장치. 두 바퀴어치를 넘기면
    # 끊는다.
    if cards and auto_cards_done >= cards * 2:
      print(f'[AUTO] 카드를 {auto_cards_done}번 했는데 안 끝나서 여기서 끊는다')
      auto_mode_done.set()
      break
    time.sleep(0.2)
  is_running = False
  memo_go.clear()
  t.join(timeout=5)
  return auto_mode_done.is_set()


_EMPTY_STUDY_JS = r"""
// 이 화면에 공부할 카드가 남아 있는지. 진행 표시가 '0/0'이면 그 구간은
// 이미 다 한 것이라 아무 일도 일어나지 않는다(자동 진행이 여기서 멈췄다).
var t = (document.body.innerText || '').replace(/\s+/g, ' ');
return /(^|\s)0\s*\/\s*0(\s|$)/.test(t);
"""


def _auto_nothing_to_study(driver):
  try:
    return bool(driver.execute_script(_EMPTY_STUDY_JS))
  except Exception:
    return False


_FIND_TEXT_EL_JS = r"""
// 화면에 보이는 요소 중 글자가 정확히 같은 것 하나를 돌려준다.
var want = arguments[0];
var els = document.querySelectorAll('a, div, label, span, button');
for (var i = 0; i < els.length; i++) {
  var e = els[i];
  if (e.children.length) continue;
  if ((e.textContent || '').replace(/\s+/g, ' ').trim() !== want) continue;
  var r = e.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) continue;
  return e;
}
return null;
"""


def _auto_click_text(driver, text):
  """그 글자가 적힌 것을 누른다."""
  try:
    el = driver.execute_script(_FIND_TEXT_EL_JS, text)
  except Exception:
    return False
  if not el:
    return False
  try:
    el.click()
  except Exception:
    try:
      driver.execute_script('arguments[0].click();', el)
    except Exception:
      return False
  return True


def _auto_handle_start_screen(driver, item, mode):
  """모드에 들어가면 먼저 나오는 '학습방법 고르고 시작' 화면을 넘긴다.

  단어 세트는 [단어 제시]로, 문장 스펠은 [어순배열]로 맞춘 뒤
  'OO 학습 시작' 버튼을 누른다. 매크로가 이 방식에 맞춰져 있다."""
  try:
    btns = [b for b in driver.find_elements(By.CSS_SELECTOR, 'a.btn-opt-start')
            if b.is_displayed()]
  except Exception:
    return False
  if not btns:
    return False
  if item.get('sentence'):
    want = '어순배열' if mode == 'spell' else None
  else:
    want = '단어 제시'
  if want:
    _auto_click_text(driver, want)
    time.sleep(0.2)
  try:
    btns[0].click()
  except Exception:
    try:
      driver.execute_script('arguments[0].click();', btns[0])
    except Exception:
      return False
  print(f'[AUTO] 학습 시작 화면 넘김 ({want or "기본 설정"})')
  time.sleep(0.7)
  return True


def _auto_study_mode(driver, mode, item, class_idx):
  """한 모드를 한 바퀴 돌린다(세트 진도 +100%).

  중요: 한 구간이 끝나면 사이트가 알아서 다음 구간으로 이어준다. 그래서 한 번
  들어가면 세트 끝까지 간다. 예전에는 그걸 모르고 구간마다 따로 들어가서,
  1구간에 들어가 놓고 세트를 통째로 한 바퀴 돌고, 다시 2구간에 들어가 또 한
  바퀴 도는 식이었다(25카드 세트가 암기 300%). 눈으로 보면 같은 단어를 계속
  반복하니 '암기를 무한정 한다'고 보였다.

  그래서 이제는 '아직 안 끝낸 첫 구간'에서 한 번만 들어간다. 목표가 100%보다
  높으면 auto_worker가 바깥에서 한 바퀴씩 더 돌린다.

  ('전체 카드로 하기'를 고르는 단추가 있었는데, 어차피 결과가 같아서 없앴다.
   구간이 다 끝나 있을 때만 아래에서 전체 카드로 한 바퀴 돈다.)"""
  segments = item.get('segments') or [1]
  seg_done = item.get('seg_done') or {}
  start = None
  for seg in segments:
    if not (seg_done.get(str(seg)) or {}).get(mode):
      start = seg
      break

  if start is not None:
    if start != segments[0]:
      print(f'[AUTO] {_MODE_NAME[mode]}: 앞 구간은 이미 100%라 {start}구간부터')
    if _auto_study_segment(driver, mode, item, class_idx, start):
      return True
    if not auto_running:
      return False
  # 구간이 다 끝나 있거나 그 구간에 할 게 없으면 전체 카드로 한 바퀴 돈다.
  print(f'[AUTO] {_MODE_NAME[mode]}: 전체 카드로 한 바퀴 돈다')
  return _auto_study_segment(driver, mode, item, class_idx, 6000)


def _auto_study_segment(driver, mode, item, class_idx, seg):
  url = f"{_SITE}/{_MODE_PATH[mode]}/{item['idx']}/{seg}/{class_idx}"
  try:
    driver.get(url)
  except Exception as e:
    _auto_log(f'주소 이동 실패: {str(e).splitlines()[0][:60]}')
    return False
  _wait_page_ready(driver, 6.0)
  time.sleep(0.4)
  _auto_handle_start_screen(driver, item, mode)
  # '0/0'은 화면이 아직 덜 그려졌을 때도 잠깐 나온다. 두 번 연속일 때만
  # 건너뛴다(5구간이 아직 안 끝났는데 건너뛴 적이 있다).
  if _auto_nothing_to_study(driver):
    time.sleep(1.2)
    if _auto_nothing_to_study(driver):
      print(f"[AUTO] {_MODE_NAME[mode]} {seg}구간은 공부할 카드가 없어 건너뜀")
      return False
  where = '전체 카드' if seg >= 1000 else f'{seg}구간'
  # 창이 가려져 있으면 크롬이 화면 그리기를 멈춰서 글자를 못 읽는다.
  # 계속 그려달라고 다시 한 번 부탁한다(탭마다 따로 해줘야 한다).
  try:
    if driver.execute_script('return document.hidden;'):
      _keep_page_awake(driver)
      time.sleep(0.4)
      if driver.execute_script('return document.hidden;'):
        _auto_log('크롬 창이 가려져 있어 화면을 못 읽습니다. 크롬 창을 열어두세요.')
  except Exception:
    pass
  _auto_log(f"{item['name']} · {_MODE_NAME[mode]} 학습 중 ({where})")

  if item.get('sentence'):
    worker = lambda: sentence_scramble_worker(_MODE_NAME[mode])
  else:
    worker = {
        'memorize': memo_worker,
        'recall': selenium_worker,
        'spell': spelling_worker,
    }[mode]
    if mode == 'memorize':
      # 단어 암기는 평소엔 사람이 [시작]을 누를 때까지 기다린다.
      memo_go.set()

  # 이 구간은 한 번만 한다.
  #
  # 예전에는 여기서 '세트 전체 진도'가 목표에 닿을 때까지 같은 구간을 계속
  # 다시 돌렸다(완료 화면의 'N% 도전'). 그런데 한 구간을 끝내봐야 세트 진도는
  # 1/구간수 만큼만 오르기 때문에, 1구간을 세 번 돌리고 2구간을 또 세 번
  # 돌리는 식이 됐다. 눈으로 보면 '암기를 무한정 반복'하는 것처럼 보였고,
  # 실제로 암기 300%가 되도록 같은 카드만 돌았다.
  # 구간을 한 바퀴 다 돌면 세트 진도가 100% 오르므로, 목표가 더 높으면
  # auto_worker가 바깥에서 한 바퀴를 더 돌린다.
  global auto_cur_idx, auto_cur_mode, auto_cards_total
  auto_cur_idx, auto_cur_mode = str(item['idx']), mode
  auto_cards_total = _study_unit_count(word_list)
  try:
    return _auto_run_worker(worker, cards=auto_cards_total)
  finally:
    auto_cur_mode = None


def _auto_study_test(driver, item, class_idx):
  """테스트(최종 시험)를 자동으로 시작하고 끝까지 푼다."""
  url = f"{_SITE}/ClassTest/{class_idx}/{item['idx']}?p=1&ex=1"
  try:
    driver.get(url)
  except Exception:
    return False
  _keep_page_awake(driver)
  time.sleep(1.5)
  _auto_log(f"{item['name']} · 테스트 시작")
  # 문제가 나오기 전에 안내 화면이 여러 장 나온다. 선생님이 만든 테스트는
  # '1차 테스트' 같은 표지가 먼저 뜨고 [다음]을 눌러야 넘어간다.
  # 화면마다 버튼 이름이 달라서(테스트 시작 / 응시 / 새로 시작 / 다음 /
  # 시작하기) 다 넣어두고, 문제 카드가 나올 때까지 계속 눌러준다.
  # 누르는 순서가 중요하다. '진행 중이던 테스트가 있습니다' 창이 떠 있으면
  # 그 창의 [응시]부터 눌러야 한다. 뒤에 가려진 [테스트 시작]을 눌러봐야
  # 화면이 안 넘어가서, 예전에는 같은 버튼을 1초에 수십 번씩 눌렀다.
  labels = ['응시', '새로 시작', '테스트 시작', '다음', '시작하기']
  misses = 0
  end = time.time() + 30
  while auto_running and time.time() < end:
    try:
      # 문제 카드가 나왔으면 그만 누른다(문제 화면의 버튼을 잘못 누르면
      # 문제를 건너뛸 수 있다).
      if driver.find_elements(By.CSS_SELECTOR, '.flip-card'):
        break
    except Exception:
      pass
    clicked = None
    for label in labels:
      if click_button_by_text(driver, [label], wait_after=0):
        clicked = label
        break
    if clicked:
      print(f'[AUTO] 테스트 시작 화면: [{clicked}] 누름')
      misses = 0
      time.sleep(1.2)   # 다음 화면이 그려질 때까지 기다린다
      continue
    misses += 1
    if misses >= 6:
      break
    time.sleep(0.4)
  if not auto_running:
    return False
  worker = (lambda: sentence_scramble_worker('테스트')) if item.get('sentence') else test_worker
  global auto_cur_idx, auto_cur_mode, auto_cards_total
  auto_cur_idx, auto_cur_mode = str(item['idx']), 'test'
  auto_cards_total = _study_unit_count(word_list)
  try:
    done = _auto_run_worker(worker, cards=auto_cards_total)
  finally:
    auto_cur_mode = None
  # 사람이 하듯 결과 화면에서 홈(클래스 화면)으로 돌아온다. 그래야 다음
  # 세트를 읽을 때 이 탭을 그대로 쓸 수 있다(안 그러면 새 탭이 생긴다).
  if auto_running:
    _open_site(driver, f'/ClassMain/{class_idx}')
    # 방금 본 테스트 점수까지 화면에 바로 반영한다.
    got = (fetch_class_report(driver, class_idx) or {}).get(str(item['idx']))
    if got:
      fresh = {m: (got.get(m) or 0) for m in ('memorize', 'recall', 'spell')}
      fresh['test'] = got.get('test') or ''
      root.after(0, lambda r=fresh, i=item['idx']: _ui_set_rate_by_idx(i, r))
  return done


def _auto_rates(driver, item, class_idx):
  """세트의 지금 진도(%)를 다시 읽는다.

  공부하던 화면을 그대로 두고 세트 페이지 내용만 받아와서 읽는다.
  (예전에는 세트 페이지로 '이동'해서 읽었는데, 학습 중에 그러다 보니 자주
  실패해서 진도가 계속 0%로 보였다. 그래서 암기가 다 끝났는데도 목표에 못
  미친 줄 알고 같은 걸 다섯 바퀴나 더 돌았다.)"""
  found = fetch_set_rates(driver, class_idx, [item['idx']])
  got = found.get(str(item['idx']))
  if not got:
    detail = fetch_set_detail(driver, item['idx'], class_idx)
    if not detail:
      return {}
    got = detail
  # 진도 표시가 아예 없으면 아직 한 번도 안 한 것이다(0%).
  return {m: (got.get(m) or 0) for m in ('memorize', 'recall', 'spell')}


def _next_auto_set(done_idx):
  """다음에 학습할 세트를 고른다. 없으면 None.

  시작할 때 목록을 못 박아두지 않고 그때그때 화면의 체크를 본다. 그래서
  학습이 도는 중에 세트를 더 체크해도 끝나는 대로 이어서 해 준다(체크를
  풀면 아직 안 한 세트는 빠진다)."""
  picked = dict(_ui_checked)  # 다른 갈래에서 바뀌는 중일 수 있어 복사해서 쓴다
  for item in list(_ui_sets):
    idx = str(item['idx'])
    if idx in done_idx or idx not in picked:
      continue
    return dict(item, target=picked[idx])
  return None


def auto_worker(plan):
  """고른 세트들을 목표 퍼센트까지 자동으로 학습한다."""
  global is_running, word_list, auto_running, auto_cur_mode
  auto_running = True
  try:
    driver = get_driver()
  except Exception as e:
    root.after(0, lambda: messagebox.showerror('자동 학습', f'크롬을 열지 못했습니다:\n{e}'))
    auto_running = False
    return

  class_idx = plan['class_idx']
  done_idx = set()
  try:
    while auto_running:
      item = _next_auto_set(done_idx)
      if item is None:
        break
      done_idx.add(str(item['idx']))
      detail = fetch_set_detail(driver, item['idx'], class_idx)
      if not detail or not detail.get('cards'):
        _auto_log(f"{item['name']}: 카드를 못 읽어서 건너뜀")
        continue
      word_list = detail['cards']
      item = dict(item)
      item['sentence'] = bool(item.get('sentence'))
      item['segments'] = detail.get('segments') or [1]
      item['seg_done'] = detail.get('seg_done') or {}
      item['rates'] = {m: detail.get(m) for m in ('memorize', 'recall', 'spell')}
      root.after(0, lambda v=('sentence' if item['sentence'] else 'word'):
                 (study_kind.set(v), on_kind_change()))
      _auto_log(f"{item['name']}: 카드 {len(word_list)}개 불러옴"
                f" ({'문장' if item['sentence'] else '단어'})")

      rates = {m: detail.get(m) or 0 for m in ('memorize', 'recall', 'spell')}
      for mode in ('memorize', 'recall', 'spell'):
        # 세트마다 목표를 따로 정할 수 있다(메인 화면의 'OO%까지').
        target = item.get('target') or (plan.get('targets') or {}).get(mode) or 0
        item['target'] = target
        rounds = 0
        # 한 번 들어가면 '구간 하나'를 끝내고 나온다(42카드 세트는 10장씩
        # 4구간). 그래서 세트 하나를 100%까지 하려면 구간 수만큼 들어갔다
        # 나와야 한다. 예전엔 5번으로 막아둬서, 구간이 6개 넘는 세트는
        # 100%를 못 채우고 끝났다.
        while auto_running and target and (rates.get(mode) or 0) < target and rounds < 12:
          rounds += 1
          _auto_log(f"{item['name']} · {_MODE_NAME[mode]} "
                    f"{rates.get(mode) or 0}% -> 목표 {target}%")
          if not _auto_study_mode(driver, mode, item, class_idx):
            break
          rates = _auto_rates(driver, item, class_idx) or rates
          root.after(0, lambda r=dict(rates), i=item['idx']:
                     _ui_set_rate_by_idx(i, r))
          # 한 바퀴를 막 끝낸 직후에는 사이트가 진도를 아직 안 올려놨을 때가
          # 있다. 그걸 '아직 모자라다'고 보고 한 바퀴를 더 도는 바람에
          # 100%면 될 것을 200%까지 했다. 모자라 보이면 잠깐씩 두고 다시 본다.
          # (통째로 2.5초 자니 모드 넘어가는 게 굼떠서 0.6초씩 세 번 본다)
          for _ in range(3):
            if not auto_running or (rates.get(mode) or 0) >= target:
              break
            time.sleep(0.6)
            rates = _auto_rates(driver, item, class_idx) or rates
            root.after(0, lambda r=dict(rates), i=item['idx']:
                       _ui_set_rate_by_idx(i, r))
      if plan['test'] and auto_running:
        # 이미 통과한 테스트는 다시 보지 않는다.
        # (예전에는 기록이 있든 없든 무조건 한 번 더 응시했다. 90점으로
        #  통과해 놓은 세트를 체크하면 또 쳐서 불편했다.)
        # 떨어진(FAIL) 기록은 '안 한 것'으로 보고 다시 친다.
        got = (fetch_class_report(driver, class_idx) or {}).get(str(item['idx'])) or {}
        prev_test = got.get('test') or ''
        if prev_test and not _test_failed(prev_test):
          _auto_log(f"{item['name']} · {_test_text(prev_test)} - 이미 통과해서 테스트는 건너뜀")
          root.after(0, lambda i=item['idx'], t=prev_test:
                     _ui_set_rate_by_idx(i, {'test': t}))
        else:
          _auto_study_test(driver, item, class_idx)
      _auto_log(f"{item['name']} 끝 (암기 {rates.get('memorize')}% /"
                f" 리콜 {rates.get('recall')}% / 스펠 {rates.get('spell')}%)")
  except Exception as e:
    err = str(e)
    root.after(0, lambda: messagebox.showerror('자동 학습', f'오류가 발생했습니다:\n{err}'))
  finally:
    auto_running = False
    is_running = False
    auto_cur_mode = None
    _auto_status('자동 학습 끝', '#388E3C')
    root.after(0, stop_macro)


def stop_auto():
  """자동 학습 정지."""
  global auto_running
  auto_running = False
  auto_mode_done.set()


# --- 단어 / 문장 전환 ---
def on_kind_change():
  """왼쪽 위 [단어]/[문장] 선택에 따라 암기 버튼이 하는 일을 바꾼다.

  리콜/스펠/테스트는 문장 단어장에서도 화면 구성이 같아서 그대로 쓴다.
  암기만 완전히 다르다 - 단어는 카드 넘기기(PyAutoGUI), 문장은 낱말을
  순서대로 클릭하는 영작 연습이다."""
  is_sentence = study_kind.get() == 'sentence'
  chosen = study_kind.get() in ('word', 'sentence')

  # 어느 쪽이 켜져 있는지 한눈에 보이도록 색/눌린 모양까지 바꾼다.
  # (라디오버튼 기본 표시는 회색 배경이라 구분이 잘 안 됐다)
  for value, button in kind_buttons.items():
    if value == study_kind.get():
      button.config(
          relief='sunken',
          bg='#D32F2F',
          fg='white',
          selectcolor='#D32F2F',
          activebackground='#D32F2F',
          activeforeground='white',
      )
    else:
      button.config(
          relief='raised',
          bg='#ECECEC',
          fg='#777777',
          selectcolor='#ECECEC',
          activebackground='#DDDDDD',
          activeforeground='#333333',
      )

  if not chosen:
    # 단어/문장을 고르기 전에는 아무것도 못 누르게 한다.
    for b in (btn_load, btn_start, btn_memo_go, btn_recall_start,
              btn_spell_start, btn_test_start):
      b.config(state=tk.DISABLED)
    lbl_status.config(text='먼저 왼쪽 위에서 [단어] 또는 [문장]을 고르세요', fg='#6A1B9A')
    return
  if not is_running:
    btn_load.config(state=tk.NORMAL)
    if lbl_status.cget('text').startswith('먼저 왼쪽 위에서'):
      lbl_status.config(text='1. 북마크 추출 -> 2. [불러오기] -> 모드 선택', fg='gray')

  frame_memo.config(text=' 암기 (영작 연습) ' if is_sentence else ' 암기 ')
  # [시작] 버튼은 단어 암기에만 필요하다. 영작 연습은 조각이 보이면 바로
  # 풀기 시작하므로 숨긴다.
  if is_sentence:
    btn_memo_go.pack_forget()
  elif not btn_memo_go.winfo_ismapped():
    btn_memo_go.pack(side='left')
  btn_start.config(
      text='영작 연습 시작' if is_sentence else '암기 시작',
      width=23 if is_sentence else 15,
      command=(lambda: run_sentence_scramble('영작 연습')) if is_sentence else start_macro,
  )

  # 문장 단어장은 리콜(듣고 배열)도 스펠도 낱말을 순서대로 누르는 방식이라
  # 영작 연습과 화면 구조가 같다. 셋 다 같은 워커를 쓴다.
  frame_recall.config(text=' 리콜 (듣고 배열) ' if is_sentence else ' 리콜 ')
  btn_recall_start.config(
      text='리콜 자동 풀이 시작',
      command=(lambda: run_sentence_scramble('리콜')) if is_sentence else run_recall_selenium,
  )

  frame_spell.config(text=' 스펠 (배열) ' if is_sentence else ' 스펠 ')
  btn_spell_start.config(
      text='스펠 자동 풀이 시작',
      command=(lambda: run_sentence_scramble('스펠')) if is_sentence else run_spelling_selenium,
  )

  # 문장 테스트(어순배열)도 같은 워커. 선생님이 문장 세트를 다시 올려줘서
  # 다시 켜고 확인 중이다.
  frame_test.config(text=' 테스트 (어순배열) ' if is_sentence else ' 테스트 ')
  test_state = tk.NORMAL if (word_list and not is_running) else tk.DISABLED
  btn_test_start.config(
      text='테스트 자동 풀이 시작',
      command=(lambda: run_sentence_scramble('테스트')) if is_sentence else run_test_selenium,
      state=test_state,
  )


# --- 메인 화면(자동 학습) 동작 ---
#
# 프로그램을 켜면 바로 이 화면이다. 왼쪽에 내 클래스, 오른쪽에 그 클래스의
# 세트가 줄줄이 나오고, 세트마다 목표 퍼센트를 정해서 [자동 시작]을 누르면
# 암기 -> 리콜 -> 스펠 -> 테스트까지 알아서 돈다.
_ui_classes = []
_ui_sets = []
_ui_rows = []
_ui_class_idx = None

# 지금 체크돼 있는 세트 {세트번호: 목표%}.
#
# 체크 표시는 화면 위젯에 들어 있는데, 자동 학습은 다른 갈래(스레드)에서
# 돌기 때문에 위젯을 직접 들여다보면 안 된다. 그래서 0.7초마다 화면을 훑어
# 이 dict에 옮겨 적고, 자동 학습은 이것만 본다. 덕분에
#  - 학습 중에 세트를 더 체크하면 그 세트도 이어서 학습하고,
#  - 목록을 다시 그려도 체크가 그대로 살아난다.
_ui_checked = {}


def _goal_value(text):
  """목표 퍼센트를 다듬는다. 최소 100, 100 단위로만 쓴다.

  사이트가 한 바퀴에 100%씩 올려주기 때문에 100보다 작거나 애매한 숫자(150 등)는
  의미가 없다. 150을 적으면 200으로 올려서 한 바퀴를 더 돌게 한다."""
  try:
    value = int(float(str(text).strip() or 100))
  except Exception:
    return 100
  if value <= 100:
    return 100
  return ((value + 99) // 100) * 100


def _ui_collect_checked():
  """지금 화면에서 체크된 세트와 목표%를 모은다. (화면 갈래에서만 부를 것)"""
  now = {}
  for row in _ui_rows:
    if not row['var'].get():
      continue
    now[str(row['item']['idx'])] = _goal_value(row['goal'].get())
  return now


def _ui_sync_checked():
  """화면의 체크/목표를 _ui_checked에 옮겨 적는다(0.7초마다)."""
  try:
    if _ui_rows:
      now = _ui_collect_checked()
      _ui_checked.clear()
      _ui_checked.update(now)
  except Exception:
    pass
  root.after(700, _ui_sync_checked)


def _ui_status(text, color='gray'):
  def work():
    try:
      lbl_auto_status.config(text=text, fg=color)
    except Exception:
      pass
  root.after(0, work)


def _ui_load_classes():
  """크롬에서 내 클래스 목록을 읽어 왼쪽에 채운다."""

  def work():
    global _ui_classes
    _ui_status('크롬에서 클래스 목록을 읽는 중...', '#0288D1')
    try:
      driver = get_driver()
    except Exception as e:
      _ui_status(f'크롬을 열지 못했습니다: {e}', '#D32F2F')
      return
    classes = fetch_classes(driver)
    if not classes and load_login():
      # 로그인이 풀린 것 같으면 저장해둔 계정으로 한 번 로그인해 본다.
      _ui_status('로그인이 풀려서 저장된 계정으로 로그인하는 중...', '#0288D1')
      if auto_login(driver):
        classes = fetch_classes(driver)
    if not classes:
      _ui_status('클래스를 못 읽었습니다. 크롬에서 로그인하거나 [자동 로그인]에 '
                 '계정을 저장하세요.', '#D32F2F')
      return
    _ui_classes = classes

    def fill():
      lst_class.delete(0, tk.END)
      for c in _ui_classes:
        lst_class.insert(tk.END, f"  {c['name']}")
      lbl_auto_status.config(text='왼쪽에서 클래스를 고르세요.', fg='gray')
      if not _ui_classes:
        return
      # [목록 새로고침]을 눌러도 보던 클래스를 그대로 다시 고른다.
      # (예전에는 무조건 첫 클래스로 돌아가서, 체크해 둔 세트가 다 사라졌다)
      pos = 0
      for i, c in enumerate(_ui_classes):
        if str(c['idx']) == str(_ui_class_idx):
          pos = i
          break
      lst_class.selection_clear(0, tk.END)
      lst_class.selection_set(pos)
      lst_class.see(pos)
      _ui_pick_class()

    root.after(0, fill)

  threading.Thread(target=work, daemon=True).start()


def _ui_pick_class(event=None):
  """고른 클래스의 세트를 읽어 오른쪽에 펼친다."""
  global _ui_class_idx
  sel = lst_class.curselection()
  if not sel or sel[0] >= len(_ui_classes):
    return
  cls = _ui_classes[sel[0]]
  _ui_class_idx = cls['idx']

  def work():
    global _ui_sets
    _ui_status(f"'{cls['name']}'의 세트를 읽는 중...", '#0288D1')
    try:
      driver = get_driver()
    except Exception as e:
      _ui_status(f'크롬을 열지 못했습니다: {e}', '#D32F2F')
      return
    sets = fetch_sets(driver, cls['idx'])
    if not sets:
      _ui_status(f"'{cls['name']}'의 세트를 못 읽었습니다. [목록 새로고침]을 눌러보세요.",
                 '#D32F2F')
      return
    _ui_sets = [dict(x, class_idx=cls['idx']) for x in sets]
    # 진도/테스트는 줄을 만든 뒤 클래스 리포트 한 장으로 채운다.
    root.after(0, _ui_build_rows)

  threading.Thread(target=work, daemon=True).start()


def _ui_build_rows():
  """세트 한 줄씩 만들어 붙인다(체크 / 이름 / 진도 / 목표).

  모든 세트를 고를 수 있다. 선생님이 지금 진도로 지정한 세트(▶)만 미리
  체크해 두고, 나머지도 체크만 하면 그대로 학습한다."""
  global _ui_rows
  for row in _ui_rows:
    row['frame'].destroy()
  _ui_rows = []

  for i, item in enumerate(_ui_sets):
    bg = '#FFFFFF' if i % 2 == 0 else '#F5F5F5'
    frame = tk.Frame(frame_sets, bg=bg)
    frame.pack(fill='x')

    # 아무것도 미리 체크하지 않는다. 지금 진도인 세트는 ▶ 와 초록 글씨로
    # 알려만 주고, 고르는 건 사용자가 한다.
    # 다만 [목록 새로고침] 등으로 줄을 다시 그릴 때는, 아까 체크해 둔 것이
    # 사라지지 않게 그대로 살려준다.
    now = bool(item.get('current') or item.get('learning'))
    var = tk.IntVar(value=1 if str(item['idx']) in _ui_checked else 0)
    kind = '문장' if item['sentence'] else '단어'
    mark = '▶ ' if now else ''
    row = {'frame': frame, 'var': var, 'item': item}

    # 오른쪽 끝(목표 %)을 먼저 붙인다. 파이썬 창은 먼저 붙인 것에 자리를
    # 먼저 주기 때문에, 이름이 길어도 목표 칸이 잘리지 않는다.
    # 목표는 화살표(위/아래)로만 조절한다. state='readonly'면 값은 그대로
    # 읽히고 화살표도 동작하는데, 사람이 직접 타자로 고칠 수는 없다.
    goal_var = tk.StringVar(
        value=str(_goal_value(_ui_checked.get(str(item['idx']))
                              or var_goal_all.get())))
    spin = tk.Spinbox(frame, from_=100, to=1000, increment=100, width=5,
                      font=('맑은 고딕', 9), justify='center',
                      textvariable=goal_var, state='readonly',
                      readonlybackground='white', cursor='hand2')
    row['goal'] = goal_var
    tk.Label(frame, text='%까지', bg=bg, font=('맑은 고딕', 9)).pack(
        side='right', padx=(0, 10))
    spin.pack(side='right')

    chk = tk.Checkbutton(
        frame,
        text=f"{mark}{item['name']}",
        variable=var,
        bg=bg,
        anchor='w',
        width=30,
        font=('맑은 고딕', 10, 'bold') if now else ('맑은 고딕', 10),
        fg='#2E7D32' if now else 'black',
        command=lambda r=row: _ui_row_toggled(r),
    )
    chk.pack(side='left', padx=(6, 0))

    tk.Label(frame, text=f"[{kind}] {item['count']}", bg=bg, fg='#777777',
             font=('맑은 고딕', 9), width=10, anchor='w').pack(side='left', padx=2)

    # 진도는 암기/리콜/스펠/테스트를 따로따로 칸을 나눠 보여준다.
    # 한 덩어리 글자로 두면 다 같은 색이라 뭐가 끝났는지 한눈에 안 보였다.
    # (끝난 것 초록 / 하다 만 것 주황 / 아직 안 한 것 회색)
    rate_box = tk.Frame(frame, bg=bg)
    rate_box.pack(side='left', padx=6, fill='x', expand=True)
    cells = {}
    for key, width in (('memorize', 9), ('recall', 9), ('spell', 9),
                       ('test', 16)):
      cell = tk.Label(rate_box, text='', bg=bg, font=('맑은 고딕', 9),
                      width=width, anchor='w')
      cell.pack(side='left')
      cells[key] = cell

    row['spin'] = spin
    row['rate'] = cells
    # 지난번에 읽어둔 진도가 있으면 먼저 연한 글씨로 보여준다(읽는 동안
    # 빈칸으로 두지 않으려고). 새로 읽으면 제 색으로 바뀐다.
    old = _rate_cache.get(str(item['idx']))
    if old:
      _ui_paint_rates(row, old, dim=True)
    _ui_rows.append(row)

  lbl_auto_status.config(
      text=f'세트 {len(_ui_sets)}개. 학습할 세트를 체크하고 목표 퍼센트를 정한 뒤 '
           '[자동 시작]을 누르세요. (▶ = 지금 진도인 세트)',
      fg='gray')
  # 진도/테스트는 체크와 상관없이 모든 세트에 표시한다(리포트 한 장으로).
  _ui_fit_scroll()
  _ui_load_report(_ui_class_idx)


def _ui_fit_scroll():
  """스크롤 범위를 내용 크기에 맞춘다."""
  frame_sets.update_idletasks()
  canvas_sets.configure(scrollregion=canvas_sets.bbox('all'))


# 세트별 진도와 테스트 결과는 클래스 '리포트' 화면 한 장에 다 들어 있다.
# 그래서 클래스를 고르면 그 한 장만 받아와서(fetch, 화면 이동 없음) 모든 줄을
# 한 번에 채운다. 세트가 80개여도 1초면 된다.
#
# 읽어둔 값은 파일에도 적어둬서, 다음에 켜면 새로 읽기 전에도 지난번 값을
# 연한 글씨로 먼저 보여준다.
_RATE_FILE = os.path.join(
    os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
    'ClasscardMacro', 'rates.json')


def _load_rate_cache():
  try:
    with open(_RATE_FILE, 'r', encoding='utf-8') as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}


_rate_cache = _load_rate_cache()


def _save_rate_cache():
  try:
    os.makedirs(os.path.dirname(_RATE_FILE), exist_ok=True)
    with open(_RATE_FILE, 'w', encoding='utf-8') as f:
      json.dump(_rate_cache, f)
  except Exception:
    pass


def _test_failed(test):
  """테스트를 보긴 했는데 떨어졌는지. 결과 줄에 'FAIL'이 붙어 있다.

  사이트는 '100점 PASS' / '40점 FAIL'처럼 적어준다. 예전에는 기록만 있으면
  다 완료(초록)로 쳤는데, 떨어진 것도 다 한 것처럼 보여서 구분한다."""
  return bool(re.search(r'fail|실패', test or '', re.I))


def _test_text(test):
  """테스트 결과 한 줄. '9/23 18:41 | 100점 PASS' -> '테스트 100점 (9/23)'.
  떨어졌으면 '테스트 40점 실패 (9/23)'."""
  test = (test or '').strip()
  if not test:
    return '테스트 안 봄'
  when, _, rest = test.partition('|')
  day = (when.strip().split() or [''])[0]
  m = re.search(r'(\d+)\s*점', rest or test)
  score = f'{m.group(1)}점' if m else ''
  tail = f' ({day})' if day else ''
  if _test_failed(test):
    return f'테스트 {score} 실패'.replace('  ', ' ') + tail
  if score:
    return f'테스트 {score}' + tail
  return '테스트 완료'


_RATE_DONE = '#2E7D32'    # 다 한 것(초록)
_RATE_PART = '#E65100'    # 하다 만 것(주황)
_RATE_NONE = '#9E9E9E'    # 아직 안 한 것(회색)
_RATE_DIM = '#C8C8C8'     # 지난번에 읽어둔 값(더 연하게)


def _rate_color(value, dim=False):
  if dim:
    return _RATE_DIM
  if not value:
    return _RATE_NONE
  return _RATE_DONE if value >= 100 else _RATE_PART


def _ui_paint_rates(row, rates, dim=False):
  """한 줄의 암기/리콜/스펠/테스트 칸을 상태에 맞는 색으로 칠한다."""
  cells = row.get('rate')
  if not isinstance(cells, dict):
    return
  rates = rates or {}
  try:
    for key, name in (('memorize', '암기'), ('recall', '리콜'), ('spell', '스펠')):
      v = rates.get(key)
      cells[key].config(text=f'{name} {_pct(v)}', fg=_rate_color(v, dim))
    test = rates.get('test')
    if dim:
      test_color = _RATE_DIM
    elif not test:
      test_color = _RATE_NONE
    elif _test_failed(test):
      test_color = _RATE_PART   # 봤지만 떨어짐 = 아직 다 한 게 아니다
    else:
      test_color = _RATE_DONE
    cells['test'].config(text=_test_text(test), fg=test_color)
  except Exception:
    pass


def _ui_paint_message(row, text, color=_RATE_NONE):
  """진도 자리에 안내 글자 한 줄만 보여준다(예: '진도를 못 읽음')."""
  cells = row.get('rate')
  if not isinstance(cells, dict):
    return
  try:
    for key in ('memorize', 'recall', 'spell', 'test'):
      cells[key].config(text='')
    cells['memorize'].config(text=text, fg=color)
  except Exception:
    pass


def _ui_row_toggled(row):
  """세트 줄의 체크를 눌렀을 때. 고른 개수만 알려준다(진도는 항상 표시)."""
  cnt = sum(1 for r in _ui_rows if r['var'].get())
  lbl_auto_status.config(text=f'세트 {cnt}개를 골랐습니다.', fg='gray')


def _ui_load_report(class_idx):
  """클래스 리포트를 읽어 모든 줄의 진도/테스트를 채운다."""

  def work():
    if auto_running or is_running:
      return  # 자동 학습이 크롬을 쓰는 중이면 건드리지 않는다
    try:
      driver = get_driver()
    except Exception:
      return
    # fetch는 클래스카드 페이지 안에서만 된다.
    try:
      if 'classcard.net' not in (driver.current_url or ''):
        _open_site(driver, '/Main/user')
    except Exception:
      pass
    data = fetch_class_report(driver, class_idx)
    if _ui_class_idx != class_idx:
      return  # 그새 다른 클래스를 골랐다
    if not data:
      root.after(0, lambda: lbl_auto_status.config(
          text='진도를 못 읽었습니다. [목록 새로고침]을 눌러보세요.', fg='#D32F2F'))
      return
    for item in _ui_sets:
      got = data.get(str(item['idx'])) or {}
      # 리포트에 없는 세트 = 아직 한 번도 안 한 세트.
      rates = {m: (got.get(m) or 0) for m in ('memorize', 'recall', 'spell')}
      rates['test'] = got.get('test') or ''
      item['rates'] = rates
      _rate_cache[str(item['idx'])] = rates
      root.after(0, lambda it=item: _ui_set_rate_text(it, it['rates']))
    _save_rate_cache()
    root.after(0, _ui_rate_progress)

  threading.Thread(target=work, daemon=True).start()


def _ui_rate_progress():
  cnt = sum(1 for r in _ui_rows if r['var'].get())
  try:
    lbl_auto_status.config(
        text=f'진도를 다 읽었습니다. 고른 세트 {cnt}개 — '
             '목표 퍼센트를 정하고 [자동 시작]을 누르세요.', fg='gray')
  except Exception:
    pass


def _pct(value):
  return f'{value}%' if value is not None else '-'


def _ui_set_rate_by_idx(set_idx, rates):
  """세트 번호로 줄을 찾아 진도를 새로 쓴다(자동 학습이 도는 중에 쓴다)."""
  for row in _ui_rows:
    if str(row['item']['idx']) != str(set_idx):
      continue
    merged = dict(row['item'].get('rates') or {})
    merged.update(rates)
    row['item']['rates'] = merged
    _rate_cache[str(set_idx)] = merged
    _ui_paint_rates(row, merged)
    return


def _ui_set_rate_text(item, rates):
  """그 세트 줄의 진도 칸을 칠한다. rates가 글자면 안내 문구로 본다."""
  for row in _ui_rows:
    if row['item'] is item:
      if isinstance(rates, str):
        _ui_paint_message(row, rates, '#D32F2F')
      else:
        _ui_paint_rates(row, rates)
      return


def _ui_check_all(on=True):
  # 체크와 진도 표시는 따로다. 체크를 풀어도 진도는 그대로 둔다.
  for row in _ui_rows:
    row['var'].set(1 if on else 0)
  cnt = sum(1 for r in _ui_rows if r['var'].get())
  lbl_auto_status.config(text=f'세트 {cnt}개를 골랐습니다.', fg='gray')


def _ui_apply_goal():
  """위쪽에 적은 목표를 모든 줄에 한 번에 넣는다(최소 100, 100 단위)."""
  value = _goal_value(var_goal_all.get())
  var_goal_all.set(str(value))
  for row in _ui_rows:
    row['goal'].set(str(value))


def _ui_start():
  """체크한 세트로 자동 학습을 시작한다."""
  if auto_running or is_running:
    messagebox.showinfo('자동 학습', '이미 진행 중입니다. 먼저 [정지]를 누르세요.')
    return
  chosen = _ui_collect_checked()
  if not chosen:
    messagebox.showwarning('자동 학습', '학습할 세트를 체크하세요.')
    return
  _ui_checked.clear()
  _ui_checked.update(chosen)

  # 어떤 세트를 할지는 여기서 못 박지 않는다. 학습이 도는 동안에도 화면의
  # 체크를 그때그때 보기 때문에, 하는 중에 세트를 더 체크하면 이어서 한다.
  plan = {
      'class_idx': _ui_class_idx,
      'targets': {},
      'test': True,
  }
  btn_auto_start.config(state=tk.DISABLED)
  btn_auto_stop.config(state=tk.NORMAL)
  lst_class.config(state=tk.DISABLED)

  def work():
    try:
      auto_worker(plan)
    finally:
      root.after(0, lambda: (btn_auto_start.config(state=tk.NORMAL),
                             btn_auto_stop.config(state=tk.DISABLED),
                             lst_class.config(state=tk.NORMAL)))

  threading.Thread(target=work, daemon=True).start()


def _ui_stop():
  stop_auto()
  stop_macro()
  _ui_status('정지했습니다.', '#D32F2F')


def open_manual_window():
  """예전 수동 조작 화면을 연다."""
  manual_win.deiconify()
  manual_win.lift()


def restart_program():
  """프로그램만 다시 시작한다. 크롬 창은 그대로 둔다(로그인 유지).

  매크로가 꼬였을 때 창을 닫고 다시 여는 수고를 덜기 위한 것이다. 새로 뜬
  프로그램은 get_driver()에서 고정 포트로 기존 크롬에 다시 붙는다.

  크롬을 조종하던 chromedriver 프로세스만 정리한다. 그냥 종료하면
  chromedriver가 주인 없이 남는다. (크롬은 detach로 띄웠으므로 같이 안 닫힌다)"""
  global is_running, shared_driver
  is_running = False
  lbl_status.config(text='재시작 중...', fg='#D32F2F')
  root.update()

  if shared_driver is not None:
    try:
      shared_driver.service.stop()
    except Exception:
      pass
    shared_driver = None

  if getattr(sys, 'frozen', False):
    # exe로 실행 중: sys.executable이 곧 암기.exe다.
    args = [sys.executable] + sys.argv[1:]
  else:
    # python main.py로 실행 중. 경로를 절대경로로 바꿔 둬야 어느 폴더에서
    # 실행했든 새 프로세스가 main.py를 찾는다.
    args = [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]

  env = dict(os.environ)
  # PyInstaller onefile exe가 자기 자신을 다시 띄우면, 새 프로세스가 지금
  # 프로세스의 임시 압축해제 폴더를 물려받았다가 우리가 종료하며 그 폴더를
  # 지우는 순간 죽는다. 이 값을 주면 새 프로세스가 처음부터 따로 시작한다.
  env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'

  try:
    subprocess.Popen(args, env=env, close_fds=True)
  except Exception as e:
    messagebox.showerror('재시작 실패', f'다시 시작하지 못했습니다:\n{e}')
    return

  root.destroy()
  os._exit(0)


def stop_macro(event=None):
  global is_running
  is_running = False
  memo_go.clear()
  btn_memo_go.config(state=tk.DISABLED)
  lbl_status.config(text='정지됨', fg='#D32F2F')
  btn_load.config(state=tk.NORMAL)
  btn_start.config(state=tk.NORMAL if word_list else tk.DISABLED)
  btn_recall_start.config(state=tk.NORMAL)
  btn_spell_start.config(state=tk.NORMAL)
  btn_test_start.config(state=tk.NORMAL)
  btn_stop.config(state=tk.DISABLED)
  on_kind_change()  # 문장 모드에서 막아둔 버튼은 계속 막아둔다


# --- UI 구성 ---
#
# 켜면 바로 보이는 화면: 왼쪽에 내 클래스, 오른쪽에 그 클래스의 세트 목록.
# 세트마다 목표 퍼센트를 정하고 [자동 시작]을 누르면 끝이다.
# 예전처럼 직접 모드를 고르고 싶으면 오른쪽 위 [수동 모드].
root = tk.Tk()
root.title('클래스카드 매크로')
root.geometry('1120x660')
# 세트 줄에 이름·진도·목표%가 한 줄에 들어가야 해서 너무 좁아지면 안 된다.
root.minsize(900, 480)
root.resizable(True, True)
root.wm_attributes('-topmost', True)
root.bind('<Escape>', stop_macro)

# ── 위쪽 띠 ────────────────────────────────────────────────
frame_head = tk.Frame(root)
frame_head.pack(fill='x', padx=14, pady=(12, 6))

tk.Label(frame_head, text='클래스카드 매크로', font=('맑은 고딕', 14, 'bold'),
         fg='#2E7D32').pack(side='left')

btn_restart = tk.Button(frame_head, text='재시작', font=('맑은 고딕', 9),
                        fg='#555555', command=lambda: restart_program())
btn_restart.pack(side='right', padx=(6, 0))

tk.Button(frame_head, text='수동 모드', font=('맑은 고딕', 9),
          command=lambda: open_manual_window()).pack(side='right', padx=6)

tk.Button(frame_head, text='목록 새로고침', font=('맑은 고딕', 9),
          command=lambda: _ui_load_classes()).pack(side='right', padx=(0, 6))

tk.Button(frame_head, text='자동 로그인', font=('맑은 고딕', 9),
          command=lambda: open_login_settings()).pack(side='right')

# ── 가운데: 클래스 목록 + 세트 목록 ────────────────────────
frame_body = tk.Frame(root)
frame_body.pack(fill='both', expand=True, padx=14)

frame_left = tk.LabelFrame(frame_body, text=' 내 클래스 ',
                           font=('맑은 고딕', 10, 'bold'))
frame_left.pack(side='left', fill='y')
lst_class = tk.Listbox(frame_left, width=22, font=('맑은 고딕', 10),
                       activestyle='none', exportselection=False,
                       selectbackground='#2E7D32', selectforeground='white')
lst_class.pack(fill='both', expand=True, padx=8, pady=8)
lst_class.bind('<<ListboxSelect>>', lambda e: _ui_pick_class())

frame_right = tk.LabelFrame(frame_body, text=' 학습할 세트 (▶ 지금 진도) ',
                            font=('맑은 고딕', 10, 'bold'))
frame_right.pack(side='left', fill='both', expand=True, padx=(10, 0))

frame_tools = tk.Frame(frame_right)
frame_tools.pack(fill='x', padx=8, pady=(8, 4))
tk.Button(frame_tools, text='모두 선택', font=('맑은 고딕', 9),
          command=lambda: _ui_check_all(True)).pack(side='left')
tk.Button(frame_tools, text='모두 해제', font=('맑은 고딕', 9),
          command=lambda: _ui_check_all(False)).pack(side='left', padx=6)

tk.Button(frame_tools, text='모두 적용', font=('맑은 고딕', 9),
          command=lambda: _ui_apply_goal()).pack(side='right', padx=(6, 18))
tk.Label(frame_tools, text='%', font=('맑은 고딕', 9)).pack(side='right')
var_goal_all = tk.StringVar(value='100')
tk.Spinbox(frame_tools, from_=100, to=1000, increment=100, width=5,
           textvariable=var_goal_all, font=('맑은 고딕', 9),
           justify='center', state='readonly', readonlybackground='white',
           cursor='hand2').pack(side='right')
tk.Label(frame_tools, text='목표 한 번에', font=('맑은 고딕', 9)).pack(
    side='right', padx=(0, 4))

# 세트 줄이 많아도 스크롤되게 캔버스 위에 올린다.
frame_scroll = tk.Frame(frame_right)
frame_scroll.pack(fill='both', expand=True, padx=8, pady=(0, 8))
canvas_sets = tk.Canvas(frame_scroll, highlightthickness=0, bg='#FFFFFF')
scroll_sets = tk.Scrollbar(frame_scroll, orient='vertical',
                           command=canvas_sets.yview)
canvas_sets.configure(yscrollcommand=scroll_sets.set)
scroll_sets.pack(side='right', fill='y')
canvas_sets.pack(side='left', fill='both', expand=True)
frame_sets = tk.Frame(canvas_sets, bg='#FFFFFF')
_sets_window = canvas_sets.create_window((0, 0), window=frame_sets, anchor='nw')
canvas_sets.bind(
    '<Configure>',
    lambda e: canvas_sets.itemconfig(_sets_window, width=e.width))
frame_sets.bind('<Configure>', lambda e: _ui_fit_scroll())
canvas_sets.bind_all(
    '<MouseWheel>',
    lambda e: canvas_sets.yview_scroll(-1 if e.delta > 0 else 1, 'units'))

# ── 아래쪽: 상태 + 시작/정지 ───────────────────────────────
lbl_auto_status = tk.Label(root, text='크롬에서 클래스 목록을 읽는 중...',
                           font=('맑은 고딕', 9), fg='gray', wraplength=900,
                           justify='center')
lbl_auto_status.pack(pady=(6, 2))

frame_run = tk.Frame(root)
frame_run.pack(pady=(0, 12))
btn_auto_start = tk.Button(frame_run, text='자동 시작', width=16, pady=4,
                           font=('맑은 고딕', 11, 'bold'), fg='white',
                           bg='#2E7D32', activebackground='#2E7D32',
                           activeforeground='white',
                           command=lambda: _ui_start())
btn_auto_start.pack(side='left', padx=6)
btn_auto_stop = tk.Button(frame_run, text='정지', width=10, pady=4,
                          font=('맑은 고딕', 11, 'bold'), fg='#C62828',
                          command=lambda: _ui_stop(), state=tk.DISABLED)
btn_auto_stop.pack(side='left', padx=6)

# ── 수동 모드 창(예전 화면) ────────────────────────────────
manual_win = tk.Toplevel(root)
manual_win.title('수동 모드')
manual_win.geometry('480x540')
manual_win.withdraw()
manual_win.protocol('WM_DELETE_WINDOW', manual_win.withdraw)
manual_win.bind('<Escape>', stop_macro)

# 단어 단어장 / 문장 단어장 전환 (왼쪽 위)
# 하나를 고르기 전에는 다른 버튼을 전부 막아둔다(on_kind_change).
study_kind = tk.StringVar(value='')
kind_buttons = {}
frame_kind = tk.Frame(manual_win)
if SENTENCE_MODE_ENABLED:
  frame_kind.pack(anchor='w', padx=15, pady=(10, 0))
for _kind_text, _kind_value in (('단어', 'word'), ('문장', 'sentence')):
  _btn = tk.Radiobutton(
      frame_kind,
      text=_kind_text,
      value=_kind_value,
      variable=study_kind,
      indicatoron=0,
      width=8,
      bd=2,
      pady=4,
      font=('맑은 고딕', 10, 'bold'),
      # 변수 값이 ''일 때 Tk가 '반쯤 선택됨' 모양으로 그리지 않게 한다.
      tristatevalue='x',
      command=lambda: on_kind_change(),
  )
  _btn.pack(side='left', padx=(0, 6))
  kind_buttons[_kind_value] = _btn

lbl_status = tk.Label(
    manual_win,
    text='크롬에서 세트를 연 뒤 [단어 불러오기]',
    font=('맑은 고딕', 9),
    fg='gray',
    wraplength=460,
    justify='center',
)
lbl_status.pack(pady=(16, 10))

frame_top = tk.Frame(manual_win)
frame_top.pack(pady=5)
btn_load = tk.Button(
    frame_top,
    text='단어 불러오기',
    width=30,
    font=('맑은 고딕', 10, 'bold'),
    fg='#0288D1',
    command=load_words,
)
btn_load.pack()

# 암기 모드 컨트롤 프레임
frame_memo = tk.LabelFrame(
    manual_win, text=' 암기 ', font=('맑은 고딕', 9, 'bold')
)
frame_memo.pack(pady=8, padx=15, fill='x')

frame_memo_row = tk.Frame(frame_memo)
frame_memo_row.pack(pady=8)

btn_start = tk.Button(
    frame_memo_row,
    text='암기 시작',
    width=15,
    font=('맑은 고딕', 9, 'bold'),
    fg='#2E7D32',
    command=start_macro,
    state=tk.DISABLED,
)
btn_start.pack(side='left', padx=(0, 6))

# 암기 화면에 들어간 뒤 사이트에서 학습을 시작하고 누르는 버튼.
btn_memo_go = tk.Button(
    frame_memo_row,
    text='시작',
    width=6,
    font=('맑은 고딕', 9, 'bold'),
    fg='#1565C0',
    command=memo_go_pressed,
    state=tk.DISABLED,
)
btn_memo_go.pack(side='left')

frame_recall = tk.LabelFrame(
    manual_win, text=' 리콜 ', font=('맑은 고딕', 9, 'bold')
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

frame_spell = tk.LabelFrame(
    manual_win, text=' 스펠 ', font=('맑은 고딕', 9, 'bold')
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

frame_test = tk.LabelFrame(
    manual_win, text=' 테스트 ', font=('맑은 고딕', 9, 'bold')
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

btn_stop = tk.Button(
    manual_win,
    text='정지',
    width=10,
    font=('맑은 고딕', 10, 'bold'),
    fg='#C62828',
    command=stop_macro,
    state=tk.DISABLED,
)
btn_stop.pack(pady=(4, 12))

if not SENTENCE_MODE_ENABLED:
  study_kind.set('word')
on_kind_change()

# 켜자마자 클래스 목록을 읽어온다.
root.after(300, _ui_load_classes)
# 체크 상태를 계속 지켜본다(학습 중에 체크해도 이어서 하도록).
root.after(1000, _ui_sync_checked)

root.mainloop()
