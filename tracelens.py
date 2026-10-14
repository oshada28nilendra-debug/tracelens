"""Offline OTLP/JSON span analysis. All times are observed, not causal."""
import argparse
from collections import Counter
import html
import json
import math
from pathlib import Path
import re
import time


def identifier(value, length):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{%d}' % length, value) or int(value, 16) == 0:
        raise ValueError('Invalid nonzero hexadecimal ID')
    return value.lower()


def timestamp(value):
    if type(value) is int:
        n = value
    elif isinstance(value, str) and re.fullmatch('[0-9]+', value):
        n = int(value)
    else:
        raise ValueError('Timestamp must be unsigned decimal nanoseconds')
    if not 0 <= n < 2**64:
        raise ValueError('Timestamp outside uint64')
    return n


def union_length(intervals):
    end = None
    total = 0
    for a, b in sorted(intervals):
        if end is None or a > end:
            total += b-a
        elif b > end:
            total += b-end
        end = max(end if end is not None else b, b)
    return total


def analyze(payload, slow_ms=100):
    started = time.perf_counter()
    if not isinstance(slow_ms, (int, float)) or not math.isfinite(slow_ms) or slow_ms < 0:
        raise ValueError('slow_ms must be finite and nonnegative')
    if not isinstance(payload, dict) or not isinstance(payload.get('resourceSpans'), list):
        raise ValueError('Expected one OTLP JSON ExportTraceServiceRequest with resourceSpans')
    spans = {}
    warnings = []
    rejected = 0
    seen = 0
    for ri, resource in enumerate(payload['resourceSpans']):
        try:
            service = 'unknown-service'
            for attr in resource.get('resource', {}).get('attributes', []):
                if attr.get('key') == 'service.name':
                    service = attr['value'].get('stringValue', service)
            if not isinstance(service, str):
                raise ValueError('service.name must be a string')
            scopes = resource.get('scopeSpans', [])
            if not isinstance(scopes, list):
                raise ValueError('scopeSpans must be an array')
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'Malformed resource {ri}: {exc}') from exc
        for scope in scopes:
            if not isinstance(scope, dict) or not isinstance(scope.get('spans', []), list):
                raise ValueError('Malformed scopeSpans entry')
            for raw in scope.get('spans', []):
                seen += 1
                if seen > 10000:
                    raise ValueError('Limit is 10000 input spans')
                try:
                    trace = identifier(raw['traceId'], 32)
                    sid = identifier(raw['spanId'], 16)
                    parent = raw.get('parentSpanId', '')
                    parent = identifier(parent, 16) if parent else None
                    start, end = timestamp(raw['startTimeUnixNano']), timestamp(raw['endTimeUnixNano'])
                    if end < start:
                        raise ValueError('End precedes start')
                    name = raw['name']
                    if not isinstance(name, str) or not name:
                        raise ValueError('Nonempty name required')
                    status = raw.get('status', {}).get('code', 0)
                    if type(status) is not int or status not in (0, 1, 2):
                        raise ValueError('status.code must be numeric 0, 1 or 2')
                    key = (trace, sid)
                    if key in spans:
                        raise ValueError('Duplicate traceId/spanId; first occurrence retained')
                    spans[key] = dict(trace_id=trace, span_id=sid, parent_id=parent, name=name,
                                      service=service, start_ns=start, end_ns=end, error=status == 2)
                except (KeyError, ValueError, TypeError, AttributeError) as exc:
                    rejected += 1
                    warnings.append(f'Input span {seen}: {exc}')
    # Resolve parents within their trace. Break cycles deterministically.
    parents = {}
    for key, span in spans.items():
        p = (key[0], span['parent_id']) if span['parent_id'] else None
        if p and p not in spans:
            warnings.append(f'Missing parent {p[1]} for {key[0]}/{key[1]}; displayed as root')
            p = None
        parents[key] = p
    complete = set()
    for key in sorted(spans):
        chain = set()
        cursor = key
        while cursor is not None and cursor not in complete:
            if cursor in chain:
                warnings.append(f'Cycle broken at {cursor[0]}/{cursor[1]}')
                parents[cursor] = None
                break
            chain.add(cursor)
            cursor = parents[cursor]
        complete.update(chain)
    children = {key: [] for key in spans}
    for key, p in parents.items():
        if p:
            children[p].append(key)
            if spans[key]['start_ns'] < spans[p]['start_ns'] or spans[key]['end_ns'] > spans[p]['end_ns']:
                warnings.append(f'Child outside parent interval: {key[0]}/{key[1]} (clock skew or async work possible)')
    traces = []
    for tid in sorted({k[0] for k in spans}):
        keys = [k for k in spans if k[0] == tid]
        origin = min(spans[k]['start_ns'] for k in keys)
        finish = max(spans[k]['end_ns'] for k in keys)
        roots = sorted((k for k in keys if parents[k] is None), key=lambda k: (spans[k]['start_ns'], k))
        stack = [(k, 0) for k in reversed(roots)]
        rows = []
        while stack:
            key, depth = stack.pop()
            s = spans[key]
            intervals = [(max(s['start_ns'], spans[c]['start_ns']), min(s['end_ns'], spans[c]['end_ns'])) for c in children[key]]
            intervals = [(a,b) for a,b in intervals if b > a]
            duration = s['end_ns']-s['start_ns']
            rows.append({**s, 'resolved_parent_id': parents[key][1] if parents[key] else None,
                         'depth': depth, 'offset_ms': (s['start_ns']-origin)/1e6,
                         'duration_ms': duration/1e6, 'uncovered_ms': (duration-union_length(intervals))/1e6,
                         'slow': duration/1e6 >= slow_ms})
            ordered = sorted(children[key], key=lambda k: (spans[k]['start_ns'], k))
            stack.extend((k, depth+1) for k in reversed(ordered))
        repeats = Counter((r['service'], r['name']) for r in rows)
        traces.append({'trace_id': tid, 'observed_duration_ms': (finish-origin)/1e6, 'spans': rows,
                       'repeated_operations': [{'service':s, 'name':n, 'count':c} for (s,n),c in sorted(repeats.items()) if c > 1]})
    ranked = sorted((r for t in traces for r in t['spans']), key=lambda r: (-r['uncovered_ms'], r['trace_id'], r['span_id']))[:20]
    return {'format': 'OTLP JSON ExportTraceServiceRequest', 'span_count':len(spans), 'rejected_span_count':rejected,
            'slow_threshold_ms':slow_ms, 'warnings':warnings, 'traces':traces, 'bottleneck_candidates':ranked,
            'analysis_seconds':time.perf_counter()-started,
            'interpretation':'Uncovered time excludes the UNION of direct-child intervals clipped to their parent. It is not CPU time or proof of a causal root cause.'}


def render(report):
    esc = lambda x: html.escape(str(x), quote=True)
    chunks = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TraceLens report</title><style>body{font:16px system-ui;background:#101924;color:#e1edf6;max-width:1100px;margin:auto;padding:28px}section{background:#192a39;padding:20px;margin:20px 0;border-radius:12px}h1{font-size:40px;color:#74ddd2}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #47606b;overflow-wrap:anywhere}.scroll{overflow:auto}.bar{height:12px;background:#64d8c8;min-width:2px}.error{background:#ee8f8f}.slow{color:#ffd582}small{color:#b9cbd9}summary{cursor:pointer}code{overflow-wrap:anywhere}</style><h1>TraceLens</h1><p>Offline distributed-trace evidence</p>']
    chunks.append('<style>*{box-sizing:border-box}body,section,li,small{overflow-wrap:anywhere;min-width:0}.scroll{max-width:100%}</style>')
    chunks.append(f'<p>{report["span_count"]} spans · {report["rejected_span_count"]} rejected · slow ≥ {report["slow_threshold_ms"]} ms</p><p>{esc(report["interpretation"])}</p>')
    chunks.append('<section><h2>Bottleneck candidates</h2><p>Ranked by observed uncovered time. Investigate with application evidence.</p><ol>')
    for s in report['bottleneck_candidates']:
        chunks.append(f'<li>{esc(s["service"])} / {esc(s["name"])} — {s["uncovered_ms"]:.3f} ms uncovered; {s["duration_ms"]:.3f} ms total <small>{esc(s["trace_id"])} / {esc(s["span_id"])}</small></li>')
    chunks.append('</ol></section><section><h2>Data quality</h2><ul>')
    chunks.extend(f'<li>{esc(w)}</li>' for w in report['warnings'])
    chunks.append('</ul></section>')
    for trace in report['traces']:
        chunks.append(f'<section><h2>Trace <code>{esc(trace["trace_id"])}</code></h2><p>{trace["observed_duration_ms"]:.3f} ms observed envelope</p><div class="scroll"><table><tr><th>Span tree</th><th>Total / uncovered ms</th><th>Status</th><th>Timeline</th></tr>')
        for s in trace['spans']:
            scale = max(trace['observed_duration_ms'], .001)
            chunks.append(f'<tr><td style="padding-left:{10+min(s["depth"],12)*16}px">{esc(s["service"])} / {esc(s["name"])}<br><small>{esc(s["span_id"])}</small></td><td>{s["duration_ms"]:.3f} / {s["uncovered_ms"]:.3f}</td><td class="slow">{("ERROR " if s["error"] else "")+("SLOW" if s["slow"] else "")}</td><td style="min-width:220px"><div class="bar {"error" if s["error"] else ""}" style="margin-left:{s["offset_ms"]/scale*100:.4f}%;width:{s["duration_ms"]/scale*100:.4f}%"></div></td></tr>')
        chunks.append('</table></div><h3>Repeated operations</h3><ul>')
        chunks.extend(f'<li>{esc(r["service"])} / {esc(r["name"])} × {r["count"]}</li>' for r in trace['repeated_operations'])
        chunks.append('</ul></section>')
    return ''.join(chunks)+'</html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, default=Path('outputs/report.json'))
    parser.add_argument('--html', type=Path, default=Path('outputs/report.html'))
    parser.add_argument('--slow-ms', type=float, default=100)
    a = parser.parse_args()
    try:
        with a.input.open('rb') as stream:
            raw = stream.read(10*1024*1024+1)
        if len(raw) > 10*1024*1024:
            raise ValueError('Input exceeds 10 MiB')
        report = analyze(json.loads(raw), a.slow_ms)
        if a.output.resolve() == a.html.resolve() or a.input.resolve() in (a.output.resolve(), a.html.resolve()):
            raise ValueError('Input, JSON output and HTML output paths must differ')
        for path, text in ((a.output,json.dumps(report,indent=2)+'\n'), (a.html,render(report))):
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(text,encoding='utf-8')
        print(f'{report["span_count"]} spans; {report["rejected_span_count"]} rejected; {len(report["warnings"])} warnings')
    except (ValueError,OSError) as exc:
        parser.exit(2,f'Error: {exc}\n')

if __name__ == '__main__':
    main()
