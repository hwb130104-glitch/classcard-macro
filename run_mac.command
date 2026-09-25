#!/bin/bash
cd "$(dirname "$0")"

echo "========================================"
echo "      클래스카드 매크로 (macOS)       "
echo "========================================"

# Python3 확인
if ! command -v python3 &> /dev/null; then
    echo "[오류] python3가 설치되어 있지 않습니다."
    echo "https://www.python.org 에서 Python을 설치해 주세요."
    read -p "종료하려면 Enter 키를 누르세요..."
    exit 1
fi

# selenium 설치 확인 및 자동 설치
python3 -c "import selenium" &> /dev/null
if [ $? -ne 0 ]; then
    echo "필요한 패키지(selenium)를 설치합니다..."
    python3 -m pip install -r requirements.txt
fi

echo "매크로를 실행합니다..."
python3 main.py
