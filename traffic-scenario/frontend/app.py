import math
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
        self.elf.state = 'idle'

    # 공통 화면의 데이터 출처 설명
    def source_note(self):
        return '트래픽 = FastAPI 실험의 실제 요청 상태'

    # 실제 요청과 장식 애니메이션만 갱신
    def update(self, dt):
        self.t += dt
        self.experiment.update(self, dt, ui)

    # 트리 양쪽에 같은 크기의 상점과 병정 공간 배치
    def layout(self, width, height):
        super().layout(width, height)
        if height >= 25:
            self.gpx = min(self.gpx, max(6, (self.bottom - 42) // 2 * 2))
        self.tree_cx = (width - 1) // 2
        self.gate_x = self.tree_cx + 2
        self.guide_top = max(0, self.bottom - 32)
        self.guide_front = (self.tree_cx - 1, min(self.bottom - 5, self.guide_top + 15))
        self.entrance = (self.guide_front[0], self.bottom + 6)
        self.queue_gap = 7
        self.qpts = [self.guide_front, self.entrance]
        self.qvis = self.bottom - self.guide_front[1] + 1
        self.qcap = int(self.qvis // self.queue_gap)
        self.booths = []
        self.shop_categories = []
        pairs = [('COOKIES', 'COCOA', ('cookie',), ('hot_choco',)),
                 ('GIFTS', 'TOY', ('gift',), ('toy',)),
                 ('ORNAMENTS', 'SANTA', ('ornament',), ('gift',)),
                 ('BAKES', 'DOLLS', ('cookie',), ('toy',))]
        for index, (left_name, right_name, left_categories, right_categories) in enumerate(pairs):
            left = (width - 9) // 2 - 14 - index * 14
            if left < 11:
                break
            for side, (x, name, categories) in enumerate(((left, left_name, left_categories),
                                                         (width - 9 - left, right_name, right_categories))):
                self.booths.append((x, name, ui.BOOTH_STRIPES[(index + side) % len(ui.BOOTH_STRIPES)]))
                self.shop_categories.append(categories)
        if len(self.booths) < 6:
            self.shop_categories[0] += ('ornament', 'gift', 'toy')
            x, _, stripes = self.booths[0]
            self.booths[0] = (x, 'GOODS', stripes)
            self.shop_categories[1] += ('cookie',)

    # 안내소 바로 아래에서 위로 이어지는 대기줄 위치 계산
    def qpos(self, distance):
        return self.guide_front[0], self.guide_front[1] + distance

    # 요청 상품 종류에 맞는 상점 앞 안내 위치 계산
    def shop_target(self, category, request_id=''):
        matches = [i for i, categories in enumerate(self.shop_categories) if category in categories] or [0]
        index = matches[sum(request_id.encode()) % len(matches)]
        return self.booths[index][0] + 3, self.gpx + 6

    # 달은 물음표처럼 보이지 않도록 둥근 모양으로 표시
    def draw(self, view='traffic'):
        canvas = super().draw(view)
        mx, my = self.w - 9, 6
        canvas.rect(mx - 3, my - 3, 7, 7, 17)
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                if dx * dx + dy * dy <= 9:
                    canvas.set(mx + dx, my + dy, 230)
        canvas.set(mx - 1, my - 1, 229)
        canvas.set(mx + 1, my + 1, 229)
        if self.experiment.celebrating():
            self.draw_fireworks(canvas)
        return canvas

    # PR 등록을 알리는 폭죽을 엘프 머리 위에서 쏘아 올려 하늘 전체에 표시
    def draw_fireworks(self, canvas):
        ex, ey = self.tree_cx - 10, self.guide_top + 2
        for burst in range(6):
            # 폭죽마다 시작 시점을 어긋나게 해 번갈아 터지게 함
            phase = ((self.t + burst * 0.45) % 2.7) / 2.7
            cx = (burst + 1) * self.w // 7
            cy = 8 + (burst * 11) % max(6, ey - 18)
            # 처음에는 엘프 위에서 터질 자리까지 불꽃이 올라감
            if phase < 0.3:
                for tail in range(5):
                    rise = max(0.0, phase / 0.3 - tail * 0.04)
                    canvas.set(int(ex + (cx - ex) * rise), int(ey + (cy - ey) * rise), 231 if tail == 0 else 226)
                continue
            spread = (phase - 0.3) / 0.7
            radius = 3 + spread * 30
            for ray in range(24):
                angle = ray * math.tau / 24
                color = 231 if spread < 0.12 else ui.LIGHTS[(burst + ray) % len(ui.LIGHTS)]
                # 끝나 가는 폭죽은 불꽃을 듬성듬성 남겨 사그라드는 모습으로 표시
                if spread > 0.7 and ray % 2:
                    continue
                for step in (1.0, 0.8, 0.6, 0.4):
                    canvas.set(int(cx + math.cos(angle) * radius * step), int(cy + math.sin(angle) * radius * step), color)

    # 대칭 노점과 중앙 트리 및 직원 한 명이 있는 안내소 표시
    def draw_market(self, canvas, top):
        for x, name, stripes in self.booths:
            canvas.rect(x, top, 9, 1, 255)
            for column in range(9):
                canvas.rect(x + column, top + 1, 1, 3, stripes[(column // 2) % 2])
            canvas.rect(x, top + 4, 9, 4, 180)
            canvas.rect(x, top + 4, 9, 2, 94)
            canvas.put(x + (9 - ui.dw(name)) // 2, (top + 4) // 2, name, 230, 94, True)
            canvas.rect(x, top + 8, 9, 1, 130)
            canvas.set(x, top + 9, 94)
            canvas.set(x + 8, top + 9, 94)
        cone = min(17, self.gpx - 3)
        for row in range(cone):
            half = int((row + 1) * .55)
            for dx in range(-half, half + 1):
                color = 22 if abs(dx) == half else 28
                if abs(dx) < half and (dx * 3 + row * 7) % 6 == 0:
                    color = ui.LIGHTS[(dx + row + int(self.t * 3)) % len(ui.LIGHTS)]
                canvas.set(self.tree_cx + dx, self.gpx - cone + row, color)
        canvas.rect(self.tree_cx - 1, self.gpx, 3, 2, 94)
        for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1)):
            canvas.set(self.tree_cx + dx, self.gpx - cone - 1 + dy, 226)

        x, y = self.tree_cx - 6, self.guide_top
        canvas.rect(x, y, 13, 3, 196)
        for column in range(0, 13, 4):
            canvas.rect(x + column, y, 2, 3, 255)
        canvas.rect(x, y + 3, 13, 9, 94)
        canvas.rect(x + 1, y + 3, 11, 7, 52)
        canvas.blit(ui.VISITOR, self.tree_cx - 1, y + 4,
                    {'H': 255, 'S': 223, 'C': 33, 'P': 24, 'k': 16})
        canvas.rect(x, y + 10, 13, 2, 180)
        sign = '버그마스 마켓 안내소'
        canvas.put(self.tree_cx - ui.dw(sign) // 2, y // 2 - 1, sign, 226, 22, True)
        # PR이 등록되면 엘프가 제자리에서 뛰어오름
        hop = 3 if self.experiment.celebrating() and int(self.t * 5) % 2 else 0
        canvas.blit(ui.ELF_SPR, x - 5, y + 4 - hop, ui.ELF_PAL)
        for person in self.queue:
            px, py = self.qpos(person.d)
            canvas.blit(ui.VISITOR[:4] + (ui.VISITOR_STEP if person.walking else ui.VISITOR[4:]), px, py, person.pal)

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


# 바뀐 줄만 한 화면 갱신으로 묶어서 출력
def frame_update(frame, previous):
    changed = [f'\x1b[{row + 1};1H{line}' for row, line in enumerate(frame)
               if row >= len(previous) or line != previous[row]]
    return '\x1b[?2026h' + ''.join(changed) + '\x1b[?2026l' if changed else ''


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
        sys.stdout.write('\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h')
        last_size = None
        previous_frame = []
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
                previous_frame = []
            frame = compose(world, size.columns, size.lines, state['paused'])
            bridge.write_state(world, state['paused'])
            sys.stdout.write(frame_update(frame, previous_frame))
            previous_frame = frame
            sys.stdout.flush()
            time.sleep(max(0, 1 / ui.FPS - (time.monotonic() - now)))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write('\x1b[?2026l\x1b[?7h\x1b[?1000l\x1b[?1006l\x1b[0m\x1b[?25h\x1b[?1049l')
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
