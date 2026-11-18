/**
 * dsh-gui —— 自创 GUI 操作插件
 *
 * 设计（取各家优点 + 我们自己的定点思路）：
 *   - 定点：控件矩形就是物理屏幕坐标，零换算，不从截图量
 *   - 新鲜：动作前重新定位控件、重取矩形；动作后校验前台/矩形/值
 *   - 直写：ValuePattern 优先，不行退键盘
 *   - 回读：写完读完都回读，不成功明说失败
 *   - 不做：审计事件、逐动作审批
 *
 * 内核是 Python 常驻服务（UIAutomation + Win32），本文件只是薄壳：
 * 注册工具 -> HTTP 转发 -> 把结果整理成模型能读的文本。
 */
import { defineTool } from '@deepseek-ai/dsh-tools';
import { spawn, execFileSync } from 'node:child_process';
import { connect } from 'node:net';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const name = 'gui';
const inject = ['tools'];

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER_DIR = join(HERE, 'gui_server');
const SERVER_PY = join(SERVER_DIR, 'gui_server.py');

let spawnPromise = null;
let spawned = null;
let lastSpawnAt = 0;
let pyCache = null;
let buildCache = { t: 0, v: '' };

/** 源码指纹：与 gui_server.py 的 _build_id() 同算法（两个文件 sha1 取前 12 位）。
 *  用来判断 8791 上跑的是不是当前代码——Windows 上父进程退出不会带走子进程，
 *  旧服务常驻会让"改了源码没生效"看起来像玄学。 */
function buildId() {
	if (Date.now() - buildCache.t < 2000) return buildCache.v;
	let v = '';
	try {
		const h = createHash('sha1');
		for (const f of ['gui_core.py', 'gui_server.py']) h.update(readFileSync(join(SERVER_DIR, f)));
		v = h.digest('hex').slice(0, 12);
	} catch {
		v = '';
	}
	buildCache = { t: Date.now(), v };
	return v;
}

function base(cfg) {
	return `http://127.0.0.1:${cfg?.port ?? 8791}`;
}

async function health(cfg) {
	try {
		const r = await fetch(base(cfg) + '/health', { signal: AbortSignal.timeout(2500) });
		if (!r.ok) return null;
		return await r.json();
	} catch {
		return null;
	}
}

async function ping(cfg) {
	return (await health(cfg)) !== null;
}

/** 端口在听就算服务活着：/health 慢不代表没起（单线程服务被一个 screen_wait 占住时就是这样） */
function portOpen(port) {
	return new Promise((resolve) => {
		let done = false;
		const sock = connect({ port, host: '127.0.0.1' }, () => fin(true));
		function fin(v) {
			if (done) return;
			done = true;
			try { sock.destroy(); } catch { /* ignore */ }
			resolve(v);
		}
		sock.on('error', () => fin(false));
		sock.setTimeout(1500, () => fin(false));
	});
}

/** 等端口释放：/shutdown 之后确认旧进程真退了，别紧接着又连上旧服务 */
async function waitPortFree(port, ms = 3000) {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		if (!(await portOpen(port))) return true;
		await new Promise((r) => setTimeout(r, 250));
	}
	return false;
}

/** 让服务自己退出（源码变了换新时用）：返回时端口已释放或已超时 */
async function shutdown(cfg) {
	try {
		await fetch(base(cfg) + '/shutdown', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: '{}',
			signal: AbortSignal.timeout(3000)
		});
	} catch {
		/* 已经没了也算成功 */
	}
	await waitPortFree(cfg?.port ?? 8791);
}

/** 找 python：配置 > 环境变量 > DSH 自带运行时 > PATH 上的 py/python。
 *  探测是同步 exec，所以只做一次并缓存。 */
function resolvePython(cfg) {
	const key = `${cfg?.python || ''}|${process.env.DSH_GUI_PYTHON || ''}`;
	if (pyCache && pyCache.key === key) return pyCache.py;
	const cands = [];
	if (cfg?.python) cands.push(cfg.python);
	if (process.env.DSH_GUI_PYTHON) cands.push(process.env.DSH_GUI_PYTHON);
	const dshHome = process.env.DSH_HOME || join(process.env.USERPROFILE || '', '.dsh');
	const rt = join(dshHome, 'dsh-runtimes');
	if (existsSync(rt)) {
		for (const d of readdirSync(rt)) {
			const p = join(rt, d, 'dependencies', 'python', 'python.exe');
			if (existsSync(p)) cands.push(p);
		}
	}
	cands.push('python.exe', 'python', 'py');
	let found = null;
	for (const c of cands) {
		try {
			const out = execFileSync(c, ['-c', 'import uiautomation, PIL; print("ok")'], {
				timeout: 6000,
				encoding: 'utf8',
				windowsHide: true
			});
			if (String(out).includes('ok')) { found = c; break; }
		} catch {
			/* try next */
		}
	}
	pyCache = { key, py: found };
	return found;
}

function launch(cfg) {
	const py = resolvePython(cfg);
	if (!py || !existsSync(SERVER_PY)) return null;
	const child = spawn(py, [SERVER_PY, String(cfg?.port ?? 8791)], {
		cwd: SERVER_DIR,
		stdio: 'ignore',
		windowsHide: true
	});
	// 必须挂 error：spawn 失败时 ChildProcess 会 emit 'error'，没有监听就是未捕获异常，能带崩宿主
	child.on('error', (e) => console.error('[dsh-gui] 拉起 Python 服务失败：' + e));
	child.on('exit', () => { if (spawned === child) spawned = null; });
	return child;
}

async function waitUp(cfg, ms) {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		await new Promise((r) => setTimeout(r, 250));
		if (await health(cfg)) return true;
		if (await portOpen(cfg?.port ?? 8791)) return true;
	}
	return false;
}

async function ensureServer(cfg) {
	const h = await health(cfg);
	const mine = buildId();
	if (h) {
		if (h.build && mine && h.build !== mine) {
			// 端口上跑的是旧代码：让它退，再拉新的（否则改完源码永远不生效）
			await shutdown(cfg);
			lastSpawnAt = 0;
		} else {
			return true;
		}
	} else if (await portOpen(cfg?.port ?? 8791)) {
		// 端口在听但 /health 不通：单线程服务可能正被长请求占着，别抢着起第二个进程
		return true;
	}
	if (spawnPromise) return await spawnPromise;
	spawnPromise = (async () => {
		if (Date.now() - lastSpawnAt < 20000) {
			// 刚拉过一次还没起来（多半在 pip 装依赖）：不并发拉第二个，等它
			return await waitUp(cfg, 12000);
		}
		lastSpawnAt = Date.now();
		spawned = launch(cfg);
		if (!spawned) return false;
		// 首次启动可能要先 pip 装依赖，给足 20s
		return await waitUp(cfg, 20000);
	})();
	try {
		return await spawnPromise;
	} finally {
		spawnPromise = null;
	}
}

async function call(cfg, path, payload = {}) {
	const ok = await ensureServer(cfg);
	if (!ok) {
		return {
			ok: false,
			error: 'SERVER_UNAVAILABLE',
			hint: `Python 服务没起来。手动跑：python "${SERVER_PY}" ${cfg?.port ?? 8791}`
		};
	}
	try {
		const res = await fetch(base(cfg) + path, {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(payload),
			signal: AbortSignal.timeout(60000)
		});
		return await res.json();
	} catch (e) {
		return { ok: false, error: 'REQUEST_FAILED', detail: String(e) };
	}
}

function targetOf(args) {
	const t = {};
	if (args.name !== undefined) t.name = args.name;
	if (args.aid !== undefined) t.aid = args.aid;
	if (args.type !== undefined) t.type = args.type;
	return t;
}

function fmtMap(v) {
	if (!v.ok) return `map failed: ${v.error ?? ''} ${JSON.stringify(v)}`;
	const w = v.window;
	const lines = [
		`WINDOW "${w.title}" class=${w.class} pid=${w.pid} rect=${w.rect.join(',')} foreground=${w.foreground}`,
		`READABILITY ${v.readability ?? '?'}`,
		...(v.window_ready === false ? ['WINDOW_READY no —— 内容区没渲染出来，别按坐标瞎点（先 screen_wait 等控件，或重开应用）'] : []),
		`HINT ${v.hint ?? ''}`,
		`CONTROLS ${v.count}`,
		'#i | type | name | automationId | rect(x1,y1,x2,y2) | center | enabled | patterns'
	];
	for (const it of v.items) {
		lines.push(
			`${it.i} | ${it.type} | ${(it.name || '').replace(/\|/g, '/').slice(0, 60)} | ${it.aid || ''} | ${it.rect.join(',')} | ${it.center.join(',')} | ${it.enabled ? 'Y' : 'N'} | ${(it.patterns || []).join('+')}`
		);
	}
	lines.push('坐标就是物理屏幕像素，可直接用于点击；target 传 name/aid/type 任意一个，动作前会重新定位并重取矩形。');
	return lines.join('\n');
}

function fmtAction(v) {
	const parts = [`ok=${v.ok}`];
	if (v.error) parts.push(`error=${v.error}`);
	if (v.via) parts.push(`via=${v.via}`);
	if (v.source) parts.push(`source=${v.source}`);
	if (v.match) parts.push(`match=${v.match}`);
	if (v.point) parts.push(`point=${v.point.join(',')}`);
	if (v.target) parts.push(`target=${JSON.stringify(v.target)}`);
	if (v.value !== undefined) parts.push(`value=${JSON.stringify(v.value)}`);
	if (v.fg_before !== undefined) parts.push(`fg_before=${v.fg_before}`);
	if (v.fg_ok !== undefined) parts.push(`fg_ok=${v.fg_ok}`);
	if (v.moved !== undefined) parts.push(`moved=${v.moved}`);
	if (v.elapsed !== undefined) parts.push(`elapsed=${v.elapsed}s`);
	if (v.detail) parts.push(`detail=${v.detail}`);
	if (v.err) parts.push(`err=${v.err}`);
	if (v.changed !== undefined && v.changed !== null) parts.push(`changed=${v.changed}`);
	if (v.changed_px !== undefined && v.changed_px !== null) parts.push(`changed_px=${v.changed_px}`);
	if (v.verified !== undefined) parts.push(`verified=${v.verified}`);
	if (v.warning) parts.push(`\n注意：${v.warning}`);
	if (v.hint) parts.push(`\n提示：${v.hint}`);
	if (v.elapsed === undefined && v.error === 'TIMEOUT') parts.push('说明：等到超时也没出现');
	return parts.join(' ');
}

const LOOSE = { type: 'object', additionalProperties: true, properties: {} };

function textOut(text) {
	return [{ type: 'text', text }];
}

const commonWindow = { type: 'string', description: '窗口标题（模糊匹配，包含即可）' };
const commonName = { type: 'string', description: '控件名字（精确优先，其次包含匹配）' };
const commonAid = { type: 'string', description: '控件 AutomationId（精确）' };
const commonType = { type: 'string', description: '控件类型：edit/button/menuitem/combobox/checkbox/listitem/treeitem/tabitem/text/pane…' };

function apply(ctx, config) {
			ctx.tools.register(
				defineTool({
					name: 'screen_map',
					description:
						'列出窗口里的控件及其真实屏幕坐标（物理像素，零换算）。每条含编号、类型、名字、AutomationId、矩形、中心点、是否可用、支持的模式。' +
						'拿到后直接用编号或名字操作，不要再去截图换算坐标。',
					parameters: {
						window: commonWindow,
						depth: { type: 'number', description: '遍历深度，默认 6' },
						include_unnamed: { type: 'boolean', description: '是否包含没有名字/ID 的容器控件，默认 false' },
						max_items: { type: 'number', description: '最多返回多少条，默认 400' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						const v = await call(config, '/map', {
							title: args.window,
							depth: args.depth ?? 6,
							include_unnamed: args.include_unnamed === true,
							max_items: args.max_items ?? 400
						});
						return { text: fmtMap(v) };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_click',
					description:
						'按控件点击：先用 name/aid/type 重新定位控件、重取当前矩形，再点中心（支持 UIA invoke 的优先 invoke，否则真鼠标）。点后校验前台窗口与矩形是否变化。' +
						'窗口移动/缩放都不怕，因为坐标是动作前现取的。自绘/网页元素读不到控件时，可传 x/y 直接点物理坐标（配合 screen_shot 的 origin 换算）。' +
						'规则：用 name/aid/type 时必须给 window（否则会从整个桌面找同名控件，可能点到别的程序）；x/y 必须成对给。',
					parameters: {
						window: commonWindow,
						name: commonName,
						aid: commonAid,
						type: commonType,
						via: { type: 'string', enum: ['auto', 'mouse', 'invoke'], description: '默认 auto' },
						button: { type: 'string', enum: ['left', 'right', 'middle'], description: '鼠标键，默认 left；右键菜单传 right' },
						double: { type: 'boolean', description: '是否双击，默认 false' },
						verify: { type: 'boolean', description: '点后自验（默认开）：对比点前后窗口像素变化，返回 changed_px/verified；changed_px=0 就是白点了。不想要开销传 false' },
						x: { type: 'number', description: '物理坐标 X（配合 y，控件读不到时用）' },
						y: { type: 'number', description: '物理坐标 Y' },
						template: { type: 'string', description: '模板图 PNG 路径：给了就先按图找位置再点（不用人眼估坐标），找不到报 TEMPLATE_NOT_FOUND、不瞎点。此时 x/y 可省略' },
						threshold: { type: 'number', description: '模板匹配阈值 0-1，默认 0.85；越小越宽松' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						const v = await call(config, '/click', {
							title: args.window,
							target: targetOf(args),
							via: args.via ?? 'auto',
							x: args.x,
							y: args.y,
							button: args.button ?? 'left',
							double: args.double ?? false,
							verify: args.verify !== false,
							template: args.template,
							threshold: args.threshold
						});
						return { text: fmtAction(v) };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_set',
					description:
						'给控件写值：优先用 UIA ValuePattern 直写（不需要焦点、不受输入法影响），写完回读比对，不一致就报失败。不适合写值的控件可退到聚焦+键盘。',
					parameters: {
						window: commonWindow,
						name: commonName,
						aid: commonAid,
						type: commonType,
						value: { type: 'string', description: '要写入的文字' },
						fallback_keys: { type: 'boolean', description: 'ValuePattern 不可用时是否退到键盘输入，默认 true' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						if (args.value === undefined || args.value === null) {
							return { text: 'ok=false error=MISSING_VALUE 必须传 value（要写入的内容）。空字符串显式清空控件' };
						}
						const v = await call(config, '/set', {
							title: args.window,
							target: targetOf(args),
							value: args.value,
							fallback_keys: args.fallback_keys !== false
						});
						return { text: fmtAction(v) };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_get',
					description: '读控件的当前值/文本（ValuePattern 或 TextPattern），用来确认状态。',
					parameters: {
						window: commonWindow,
						name: commonName,
						aid: commonAid,
						type: commonType
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => true,
					async execute(args) {
						const v = await call(config, '/get', { title: args.window, target: targetOf(args) });
						let text;
						if (v.ok) text = `ok=true value=${JSON.stringify(v.value)} text=${JSON.stringify((v.text || '').slice(0, 800))}`;
						else text = fmtAction(v);
						return { text };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_type',
					description:
						'逐字输入文字（真键盘 SendInput）：默认 unicode 模式，中文/符号直接打、不依赖输入法；mode=vk 走虚拟键路径（给 VMware 这类不认 UNICODE 的环境）。可先聚焦指定控件，打完回读值核对。' +
						'窗口顶不到前台时直接报 WINDOW_NOT_FOREGROUND 不发键——免得字打进别的窗口。',
					parameters: {
						window: commonWindow,
						text: { type: 'string', description: '要输入的文字' },
						name: commonName,
						aid: commonAid,
						type: commonType,
						mode: { type: 'string', enum: ['unicode', 'vk'], description: '默认 unicode' },
						delay_ms: { type: 'number', description: '每字间隔毫秒，默认 4' },
						enter: { type: 'boolean', description: '打完是否回车，默认 false' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						const v = await call(config, '/type', {
							title: args.window,
							target: targetOf(args),
							text: args.text ?? '',
							mode: args.mode ?? 'unicode',
							delay_ms: args.delay_ms ?? 4,
							enter: args.enter === true
						});
						let text;
						if (v.ok) {
							text =
								`ok=true sent=${v.sent} mode=${v.mode} elapsed=${v.elapsed}s` +
								(v.value !== null && v.value !== undefined ? ` value=${JSON.stringify(v.value)}` : '');
						} else text = fmtAction(v);
						return { text };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_key',
					description:
						'发按键/组合键（真键盘 SendInput）：enter / tab / esc / 方向键 / f5 / ctrl+a / ctrl+shift+end 等，可一次多组、可重复。先激活窗口再发。',
					parameters: {
						window: commonWindow,
						keys: {
							type: 'array',
							items: { type: 'string' },
							description: '按键列表，如 ["ctrl+a","enter"]；单个字符串也接受'
						},
						repeat: { type: 'number', description: '重复次数，默认 1' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						const keys = Array.isArray(args.keys) ? args.keys : args.keys ? [args.keys] : [];
						const v = await call(config, '/key', {
							title: args.window,
							keys,
							repeat: args.repeat ?? 1
						});
						const text = v.ok
							? `ok=true sent=${JSON.stringify(v.sent)} elapsed=${v.elapsed}s`
							: fmtAction(v);
						return { text };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_wait',
					description: '等窗口或控件出现，出现立即返回并给出耗时；超时如实报 TIMEOUT。用来替代固定 sleep。',
					parameters: {
						window: commonWindow,
						name: { type: 'string', description: '要等的控件名（可省略，只等窗口）' },
						timeout: { type: 'number', description: '最多等多少秒，默认 10（上限 55：服务是单线程的，等太久会把别的请求堵住）' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => true,
					async execute(args) {
						const payload = { title: args.window, timeout: args.timeout ?? 10 };
						const t = targetOf(args);
						if (Object.keys(t).length) payload.target = t;
						const v = await call(config, '/wait', payload);
						const text = v.ok
							? `ok=true 等到了，耗时 ${v.elapsed}s ${v.control ? 'control=' + JSON.stringify(v.control) : 'window=' + JSON.stringify(v.window)}`
							: `ok=false ${v.error ?? 'UNKNOWN'} 耗时 ${v.elapsed ?? '?'}s`;
						return { text };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_calibrate',
					description:
						'枚举显示器：每块屏的物理矩形、DPI、缩放系数，另给虚拟桌面范围和进程 DPI 感知级别。换机器/多屏/高 DPI 时先跑这个确认坐标基准；锁屏也能读。',
					parameters: {},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => true,
					async execute() {
						const v = await call(config, '/calibrate', {});
						if (!v.ok) return { text: fmtAction(v) };
						const lines = [
							`ok=true monitors=${v.count} virtual_rect=${(v.virtual_rect || []).join(',')} system_dpi=${v.system_dpi} process_dpi_aware=${v.process_dpi_aware}`
						];
						for (const m of v.monitors || []) {
							lines.push(
								`  ${m.primary ? '*' : ' '} ${m.device} rect=${m.rect.join(',')} size=${m.size.join('x')} dpi=${m.dpi.join('x')} scale=${m.scale}`
							);
						}
						lines.push(v.note || '');
						return { text: lines.join('\n') };
					}
				})
			);
		
			ctx.tools.register(
				defineTool({
					name: 'screen_shot',
					description:
						'截一张 1:1 物理像素的图，返回路径和这张图覆盖的物理矩形（origin/rect）。图内坐标 + origin = 屏幕物理坐标——换机器、多屏、高 DPI 都按这个换算，不要假设 DPI。factor：图内坐标 ×1 即物理，不缩放。',
					parameters: {
						window: { type: 'string', description: '窗口标题；省略则截整个虚拟桌面' },
						region: {
							type: 'array',
							items: { type: 'number' },
							description: '截取区域 [x1,y1,x2,y2]（物理像素），给了就优先于 window'
						},
						annotate: { type: 'boolean', description: '是否把控件编号标在图上（配合 screen_map 看）' },
						grid: { type: 'boolean', description: '叠加坐标网格（红线每 500px 并标注屏幕坐标），看图读数用' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => true,
					async execute(args) {
						const v = await call(config, '/shot', {
							title: args.window,
							region: Array.isArray(args.region) ? args.region : undefined,
							annotate: args.annotate === true,
							grid: args.grid === true,
							out_path: join(process.env.TEMP || '.', `dsh-gui-${Date.now()}.png`)
						});
						const text = v.ok
							? `ok=true path=${v.path} source=${v.source ?? 'screen'} size=${(v.size || []).join('x')} origin=${(v.origin || []).join(',')} rect=${(v.rect || []).join(',')} annotated=${v.annotated} items=${v.items}`
							: fmtAction(v);
						return { text };
					}
				})
			);
			ctx.tools.register(
				defineTool({
					name: 'screen_find',
					description:
						'模板匹配找图：给一张小图（按钮/图标/截下来的局部），在屏幕上找到它并返回物理坐标。' +
						'专治"读不到控件、但长相固定"的元素，比人眼从截图里估坐标准。用法：先 screen_shot 截元素局部当模板，再调本工具拿 point。',
					parameters: {
						template: { type: 'string', description: '模板图路径（PNG，建议只截元素本身，别截全屏）' },
						threshold: { type: 'number', description: '匹配阈值 0-1，默认 0.85；越小越宽松' },
						window: { type: 'string', description: '只在这个窗口的像素里找（窗口被挡住也能找）；省略则在屏幕上找' },
						region: {
							type: 'array',
							items: { type: 'number' },
							description: '限定搜索区域 [x1,y1,x2,y2]（物理像素，必须 4 个），省略则整屏'
						}
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => true,
					async execute(args) {
						const v = await call(config, '/find', {
							template: args.template,
							threshold: args.threshold ?? 0.85,
							window: args.window,
							region: Array.isArray(args.region) ? args.region : undefined
						});
						const text = v.ok
							? `ok=true score=${v.score} source=${v.source ?? 'screen'} point=${(v.point || []).join(',')} rect=${(v.rect || []).join(',')}`
							: fmtAction(v);
						return { text };
					}
				})
			);
			ctx.tools.register(
				defineTool({
					name: 'screen_hover',
					description:
						'把鼠标停到控件/坐标上等一会儿，并报告这期间新冒出来的弹层（子菜单/提示框）。' +
						'专治三级菜单：hover 一级拿回子菜单项的名字和坐标，再对下一级 hover/click，不用猜悬停时长、不用整屏截图找菜单。',
					parameters: {
						window: commonWindow,
						name: commonName,
						aid: commonAid,
						type: commonType,
						x: { type: 'number', description: '物理坐标 X（读不到控件时用，配合 y）' },
						y: { type: 'number', description: '物理坐标 Y' },
						wait: { type: 'number', description: '悬停多久等弹层，默认 0.6 秒' },
						activate: { type: 'boolean', description: '窗口不在前台时是否先顶到前台，默认 true' },
						detect_popup: { type: 'boolean', description: '是否报告新弹层，默认 true' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						const v = await call(config, '/hover', {
							title: args.window,
							target: targetOf(args),
							x: args.x,
							y: args.y,
							wait: args.wait ?? 0.6,
							activate: args.activate !== false,
							detect_popup: args.detect_popup !== false
						});
						const lines = [fmtAction(v)];
						if (v.ok && Array.isArray(v.popups) && v.popups.length) {
							lines.push(`POPUPS ${v.popup_count}`);
							for (const p of v.popups) {
								lines.push(`class=${p.class} hwnd=${p.hwnd} rect=${(p.rect || []).join(',')} items=${p.count}`);
								for (const it of p.items || []) {
									lines.push(`  ${it.name} | ${it.type} | center=${(it.center || []).join(',')}`);
								}
							}
						}
						return { text: lines.join('\n') };
					}
				})
			);
			ctx.tools.register(
				defineTool({
					name: 'screen_scroll',
					description:
						'滚轮：指针放到控件/坐标上，往 up/down/left/right 滚 amount 格。' +
						'能读到滚动位置时返回 scroll_before/after/delta 与 verified，明确告诉你滚没滚；读不到就 verified=null，别当成功。',
					parameters: {
						window: commonWindow,
						name: commonName,
						aid: commonAid,
						type: commonType,
						x: { type: 'number', description: '物理坐标 X（读不到控件时用）' },
						y: { type: 'number', description: '物理坐标 Y' },
						direction: { type: 'string', description: 'up / down / left / right，默认 down' },
						amount: { type: 'number', description: '滚多少格（1 格=120），默认 3' },
						verify: { type: 'boolean', description: '动作前后读滚动百分比做校验，默认 true' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						const v = await call(config, '/scroll', {
							title: args.window,
							target: targetOf(args),
							x: args.x,
							y: args.y,
							direction: args.direction ?? 'down',
							amount: args.amount ?? 3,
							verify: args.verify !== false
						});
						return { text: fmtAction(v) };
					}
				})
			);
			ctx.tools.register(
				defineTool({
					name: 'screen_drag',
					description:
						'拖拽：把鼠标从起点按到终点（分步移动，一次跳过去很多程序会当成单击）。' +
						'起点终点可给控件名或物理坐标（必须明给，不会默认从窗口中心拖）。返回 changed_px / verified / window_moved，拖没拖动有据可查。',
					parameters: {
						window: commonWindow,
						from_name: { type: 'string', description: '起点控件名（精确优先，其次包含）' },
						from_x: { type: 'number', description: '起点物理坐标 X（读不到控件时用）' },
						from_y: { type: 'number', description: '起点物理坐标 Y' },
						to_name: { type: 'string', description: '终点控件名' },
						to_x: { type: 'number', description: '终点物理坐标 X' },
						to_y: { type: 'number', description: '终点物理坐标 Y' },
						duration_ms: { type: 'number', description: '整个过程耗时，默认 400ms（太快会被当成单击）' },
						steps: { type: 'number', description: '分几步移动，默认 16' },
						button: { type: 'string', description: 'left / right / middle，默认 left' },
						verify: { type: 'boolean', description: '拖完对比目标点周围画面，默认 true' }
					},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => false,
					async execute(args) {
						const from = {};
						if (args.from_name) from.target = { name: args.from_name };
						if (args.from_x !== undefined && args.from_y !== undefined) { from.x = args.from_x; from.y = args.from_y; }
						const to = {};
						if (args.to_name) to.target = { name: args.to_name };
						if (args.to_x !== undefined && args.to_y !== undefined) { to.x = args.to_x; to.y = args.to_y; }
						const v = await call(config, '/drag', {
							title: args.window,
							from,
							to,
							duration_ms: args.duration_ms ?? 400,
							steps: args.steps ?? 16,
							button: args.button ?? 'left',
							verify: args.verify !== false
						});
						const extra = [];
						if (v.ok) {
							extra.push(`from=${(v.from || []).join(',')} to=${(v.to || []).join(',')}`);
							if (v.window_rect_before && v.window_rect_after) {
								extra.push(`win_before=${(v.window_rect_before || []).join(',')} win_after=${(v.window_rect_after || []).join(',')} window_moved=${v.window_moved}`);
							}
						}
						return { text: [fmtAction(v), ...extra].join('\n') };
					}
				})
			);
			ctx.tools.register(
				defineTool({
					name: 'screen_windows',
					description:
						'列出所有可见顶层窗口（标题/类名/PID/矩形），包括对话框和弹出菜单。' +
						'找不到窗口、或不确定标题时先调这个，别瞎猜。',
					parameters: {},
					output: { schema: LOOSE, render: (_a, v) => textOut(v.text) },
					isConcurrencySafe: () => true,
					async execute() {
						const v = await call(config, '/windows', {});
						if (!v.ok) return { text: fmtAction(v) };
						const ws = v.windows || [];
						const lines = [`WINDOWS ${ws.length}`];
						for (const w of ws) {
							lines.push(`${(w.class || '').slice(0, 26)} | pid=${w.pid} | ${(w.title || '').slice(0, 52)} | rect=${(w.rect || []).join(',')}`);
						}
						lines.push('window= 传标题里的关键词即可（模糊匹配，包含就中）');
						return { text: lines.join('\n') };
					}
				})
			);
		
}

export { apply, inject, name };
