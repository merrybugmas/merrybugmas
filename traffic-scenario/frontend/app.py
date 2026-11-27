import argparse
import json
import os
from pathlib import Path
import select
import sys
import time

from .session import Session

# 공통 렌더러는 수정 없이 불러오기
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import xmas_market as ui

ROOT = Path(__file__).resolve().parents[1]


class TrafficWorld(ui.World):
    # 기존 픽셀 자원으로 트래픽 전용 화면 준비
    def __init__(self, session):
        super().__init__()
        self.experiment = session
        self.auto = False
        self.start_stage(0, 'traffic')

    # 공통 화면의 데이터 출처 설명
    def source_note(self):
        return '트래픽 = FastAPI 실험의 실제 요청 상태'

    # 실제 요청과 장식 애니메이션만 갱신
    def update(self, dt):
        self.t += dt
        self.experiment.update(self, dt, ui)

    # 안내소 간판을 트래픽 화면에서만 표시
    def draw_market(self, canvas, top):
        super().draw_market(canvas, top)
        canvas.put(self.gate_x - 1, (self.bottom - 16) // 2, ' 안내소 ', 226, 22, True)
        # 직원은 요청 상태와 관계없이 안내소 안에 고정 표시
        canvas.rect(self.gate_x + 2, self.bottom - 8, 5, 6, 52)
        canvas.blit(ui.VISITOR, self.gate_x + 3, self.bottom - 8,
                    {'H': 255, 'S': 223, 'C': 33, 'P': 24, 'k': 16})

    # 채팅 명령을 트래픽 실험에 전달
    def command(self, line):
        parts = line.strip().split(None, 1)
        if not parts:
            return
        if parts[0] == 'experiment' and len(parts) > 1:
            self.experiment.command(parts[1])
        elif parts[0] == 'say' and len(parts) > 1:
            self.log(parts[1], 183, 'traffic')
        else:
            self.log('트래픽 명령: experiment normal / surge / stop / replay / compare', 223, 'traffic')

    # 기존 상태 형식에 실제 실험 정보 추가
    def snapshot_state(self):
        value = super().snapshot_state()
        value['experiment'] = self.experiment.status()
        value['traffic'].pop('elf', None)
        value['live']['traffic'] = 'experiment'
        return value


# 공통 화면의 픽셀 렌더러로 트래픽 화면 구성
def compose(world, width, height, paused=False):
    world.hits = []
    if width < 60 or height < 22:
        return [ui.seg_line([(f'60x22 이상 필요 (현재 {width}x{height})', 231)], width, 52)] + [' ' * width] * (height - 1)
    return world.experiment.render(world, width, height, paused, ui)


# 트래픽 화면의 키 입력 처리
def handle_key(world, key, state):
    key = key.lower()
    if key == 'q':
        state['quit'] = True
    elif key == ' ':
        state['paused'] = not state['paused']
    else:
        world.experiment.handle_key(key)


# 공통 마우스와 방향키 형식으로 입력 해석
def handle_input(world, buffer, state):
    i = 0
    while i < len(buffer):
        if buffer[i] != '\x1b':
            handle_key(world, buffer[i], state)
            i += 1
            continue
        mouse = ui.MOUSE.match(buffer, i)
        if mouse:
            button, x, y = (int(mouse.group(n)) for n in (1, 2, 3))
            if mouse.group(4) == 'M' and button == 0:
                for x0, x1, y0, y1, action in list(world.hits):
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        action()
                        break
            i = mouse.end()
            continue
        escape = ui.CSI.match(buffer, i)
        if escape:
            sequence = escape.group(0)
            if len(sequence) == 3 and sequence[2] in ui.ARROWS:
                handle_key(world, ui.ARROWS[sequence[2]], state)
            i = escape.end()
            continue
        if buffer[i:i + 2] == '\x1bO' and i + 2 < len(buffer):
            handle_key(world, ui.ARROWS.get(buffer[i + 2], ''), state)
            i += 3
            continue
        if i == len(buffer) - 1:
            handle_key(world, '\x1b', state)
            i += 1
        elif ui.re.fullmatch(r'\x1b\[[0-9;<?]*', buffer[i:]):
            return buffer[i:]
        else:
            i += 1
    return ''


# 채팅 명령과 일시 정지 요청 처리
def read_commands(world, bridge, state):
    for line in bridge.read_commands():
        if line.strip().lower() in ('pause', 'resume'):
            state['paused'] = line.strip().lower() == 'pause'
        else:
            world.command(line)


# 네이티브 터미널에서 마켓 실행
def run_terminal(world, runtime):
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    bridge = ui.Bridge(str(runtime))
    state = {'quit': False, 'paused': False}
    pending = ''
    previous = time.monotonic()
    try:
        tty.setcbreak(fd)
        sys.stdout.write('\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h')
        last_size = None
        while not state['quit']:
            now = time.monotonic()
            dt, previous = min(.2, now - previous), now
            while select.select([fd], [], [], 0)[0]:
                pending = handle_input(world, pending + os.read(fd, 1024).decode('utf-8', 'ignore'), state)
            read_commands(world, bridge, state)
            if not state['paused']:
                world.update(dt)
            size = os.get_terminal_size()
            if size != last_size:
                sys.stdout.write('\x1b[2J')
                last_size = size
            frame = compose(world, size.columns, size.lines, state['paused'])
            bridge.write_state(world, state['paused'])
            sys.stdout.write('\x1b[H' + '\r\n'.join(frame))
            sys.stdout.flush()
            time.sleep(max(0, 1 / ui.FPS - (time.monotonic() - now)))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write('\x1b[?1000l\x1b[?1006l\x1b[0m\x1b[?25h\x1b[?1049l')
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# 기존 옆 패널 형식으로 트래픽 프레임 출력
def run_stream(world, runtime, width, height):
    bridge = ui.Bridge(str(runtime))
    state = {'quit': False, 'paused': False}
    try:
        old_lines = (runtime / 'pane_cmd').read_text().splitlines()
    except OSError:
        old_lines = []
    sequence = max([int(line.partition('\t')[0]) for line in old_lines if line.partition('\t')[0].isdigit()] or [-1])
    previous = time.monotonic()
    last = None
    while not state['quit']:
        now = time.monotonic()
        dt, previous = min(.2, now - previous), now
        try:
            lines = (runtime / 'pane_cmd').read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            number, separator, command = line.partition('\t')
            if not separator or not number.isdigit() or int(number) <= sequence:
                continue
            sequence = int(number)
            op, _, arg = command.partition(' ')
            if op == 'key':
                handle_key(world, ui.PANE_KEYS.get(arg.lower(), arg), state)
            elif op == 'click':
                try:
                    x, y = (int(v) + 1 for v in arg.split())
                except ValueError:
                    continue
                for x0, x1, y0, y1, action in list(world.hits):
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        action()
                        break
            elif op == 'size':
                try:
                    width, height = (max(20, int(v)) for v in arg.lower().split('x'))
                except ValueError:
                    continue
            else:
                world.command(command)
        read_commands(world, bridge, state)
        if not state['paused']:
            world.update(dt)
        frame = compose(world, width, height, state['paused'])
        bridge.write_state(world, state['paused'])
        value = json.dumps({'cols': width, 'blocks': ui.frame_blocks(frame), 'caught': [], 'hist': False, 'prs': []}, ensure_ascii=False)
        if value != last:
            try:
                print(value, flush=True)
            except (BrokenPipeError, OSError):
                return
            last = value
        time.sleep(max(0, 1 / 8 - (time.monotonic() - now)))


# 같은 서버에서 진행 중인 실험에만 다시 연결
def saved_run(runtime, server):
    try:
        value = json.loads((runtime / 'state.json').read_text()).get('experiment') or {}
        run = value.get('run') or {}
        if run.get('status') not in ('pending', 'running', 'stopping'):
            return None
        return value.get('run_id') if value.get('server') == server.rstrip('/') else None
    except (OSError, ValueError, AttributeError):
        return None


# 트래픽 폴더 안에서 프론트 실행
def main():
    parser = argparse.ArgumentParser(description='Merry Bug Mas 트래픽 전용 마켓')
    parser.add_argument('--server', default='http://127.0.0.1:8000')
    parser.add_argument('--run-id')
    parser.add_argument('--stream', action='store_true')
    parser.add_argument('--size', default='100x30')
    args = parser.parse_args()
    if not args.stream and not sys.stdin.isatty():
        parser.error('터미널에서 실행하세요. 옆 패널은 --stream을 사용하세요.')
    runtime = Path(os.environ.get('XMAS_MARKET_RUNTIME', ROOT / '.runtime'))
    session = Session(args.server, args.run_id or saved_run(runtime, args.server))
    world = TrafficWorld(session)
    session.start()
    try:
        if args.stream:
            width, height = (int(v) for v in args.size.lower().split('x'))
            run_stream(world, runtime, width, height)
        else:
            run_terminal(world, runtime)
    finally:
        session.close()
