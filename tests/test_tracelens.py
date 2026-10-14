import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from sample import fixture
from tracelens import analyze, render, union_length

class TraceTests(unittest.TestCase):
    def test_expected_metrics(self):
        r=analyze(fixture())
        rows={s['span_id']:s for s in r['traces'][0]['spans']}
        self.assertEqual(r['span_count'],6)
        self.assertEqual(rows[f'{2:016x}']['uncovered_ms'],70)
        self.assertEqual(rows[f'{1:016x}']['uncovered_ms'],40)
        self.assertEqual(r['bottleneck_candidates'][0]['name'],'charge')
        self.assertEqual(r['traces'][0]['observed_duration_ms'],540)
        self.assertEqual(r['traces'][0]['repeated_operations'][0]['count'],2)
        self.assertEqual(sum(s['error'] for s in rows.values()),1)
        self.assertIn('Missing parent',r['warnings'][0])

    def test_union_overlap(self):
        self.assertEqual(union_length([(0,10),(5,15),(3,6),(20,21)]),16)
        self.assertEqual(union_length([]),0)

    def test_malformed_span_retained_diagnostics(self):
        p=fixture();p['resourceSpans'][0]['scopeSpans'][0]['spans'].append({'name':'broken'})
        r=analyze(p);self.assertEqual(r['rejected_span_count'],1);self.assertEqual(r['span_count'],6)

    def test_duplicate(self):
        p=fixture();sp=p['resourceSpans'][0]['scopeSpans'][0]['spans'];sp.append(copy.deepcopy(sp[0]))
        self.assertEqual(analyze(p)['rejected_span_count'],1)

    def test_cycle(self):
        p=fixture();p['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['parentSpanId']=f'{2:016x}'
        r=analyze(p);self.assertEqual(len(r['traces'][0]['spans']),6)
        self.assertTrue(any('Cycle' in w for w in r['warnings']))

    def test_trace_isolation(self):
        p=fixture();q=copy.deepcopy(p['resourceSpans'][0]);q['scopeSpans'][0]['spans'][0]['traceId']='b'*32
        p['resourceSpans'].append(q);r=analyze(p);self.assertEqual(len(r['traces']),2);self.assertEqual(r['span_count'],7)

    def test_bad_timestamps(self):
        for value in [True,1.5,'nan','-1',str(2**64)]:
            with self.subTest(value=value):
                p=fixture();p['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['endTimeUnixNano']=value
                self.assertEqual(analyze(p)['rejected_span_count'],1)

    def test_invalid_top_level(self):
        for value in [[],{},None,{'resourceSpans':{}},{'resourceSpans':[None]}]:
            with self.subTest(value=value),self.assertRaises(ValueError):analyze(value)

    def test_bad_threshold(self):
        for value in [-1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):analyze(fixture(),value)

    def test_html_escaped(self):
        p=fixture();p['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['name']='<script>alert(1)</script>'
        page=render(analyze(p));self.assertNotIn('<script>',page);self.assertIn('&lt;script&gt;',page)

    def test_empty_and_zero_duration(self):
        self.assertEqual(analyze({'resourceSpans':[]})['traces'],[])
        p=fixture();s=p['resourceSpans'][0]['scopeSpans'][0]['spans'][0];s['endTimeUnixNano']=s['startTimeUnixNano']
        r=analyze(p);self.assertTrue(any('outside parent' in w for w in r['warnings']))
        self.assertTrue(all(s['uncovered_ms']>=0 for s in r['traces'][0]['spans']))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);src=root/'input.json';src.write_text(json.dumps(fixture()))
            args=[sys.executable,'tracelens.py',str(src),'--output',str(root/'report.json'),'--html',str(root/'report.html')]
            result=subprocess.run(args,capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads((root/'report.json').read_text())['span_count'],6)
            src.write_text('{');self.assertEqual(subprocess.run(args,capture_output=True).returncode,2)

if __name__=='__main__':unittest.main()
