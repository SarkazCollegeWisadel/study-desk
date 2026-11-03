"""Render local-only catalog metadata with source links; no external resources."""
from pathlib import Path
import html

def render(output,catalog,outlines,roots):
    esc=lambda value:html.escape(str(value or ''),quote=True)
    sources={s['source_id']:s for s in catalog['sources']}
    def link(sid):
        source=sources.get(sid)
        if not source:return '<span>缺少来源</span>'
        loc=source['locations'][0];path=Path(roots[loc['root']])/loc['path']
        return '<a href="'+esc(path.resolve().as_uri())+'">'+esc(source['title'])+'</a>'
    cards=[]
    for topic in outlines['diagnostics_review']['topics']:
        refs=[]
        for row in topic['citations']:
            pages=', '.join(row['physical_pdf_pages']) or '未标页'
            refs.append('<li>'+link(row['source_id'])+'<br><small>'+esc(row['heading'])+' · 原文行 '+esc(str(row['start_line'])+'–'+str(row['end_line']))+' · PDF物理页 '+esc(pages)+'</small><br>'+link(row['original_source_id'])+'</li>')
        cards.append('<article><h3>'+esc(topic['topic'])+'</h3><ol>'+''.join(refs)+'</ol></article>')
    courses=[]
    for course in outlines['courses']:
        formal=course['formal_source_ids'];status='已存版本受限的大纲，须核对适用性' if formal else '本轮已核范围未取得学校正式大纲'
        nodes=course['nodes'];scope=link(course['chapter_scope_source_id']) if course['chapter_scope_source_id'] else '尚无章节目录'
        details=''.join('<li>'+esc(n['number'])+' '+esc(n['title'])+' <small>原文行 '+esc(n['start_line'])+'–'+esc(n['end_line'])+'</small></li>' for n in nodes)
        courses.append('<details><summary>'+esc(course['subject'])+' · '+esc(status)+' · '+str(len(nodes))+'个目录节点</summary><p>'+scope+'</p><p>'+esc(course['limitation'])+'</p><ul>'+details+'</ul></details>')
    assets=[]
    for source in catalog['sources']:
        assets.append('<tr><td>'+link(source['source_id'])+'</td><td>'+esc(source['document_type'])+'</td><td>'+esc('；'.join(source['versions']))+'</td><td><code>'+esc(source['sha256'])+'</code></td></tr>')
    document='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>复习来源目录</title><style>body{margin:0;background:#f5f6f8;color:#20252b;font:16px/1.6 system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:32px 24px}h1{margin-bottom:8px}a{color:#165da6;overflow-wrap:anywhere}small{color:#59636f}article,details{background:white;border:1px solid #dbe0e6;border-radius:10px;padding:18px;margin:12px 0}h3{margin-top:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}li{margin:10px 0}.notice{padding:14px 18px;background:#fff5d9;border-left:4px solid #c28d1b}table{width:100%;border-collapse:collapse;background:white;font-size:13px}td,th{padding:10px;border:1px solid #dbe0e6;text-align:left;vertical-align:top}code{overflow-wrap:anywhere;word-break:break-all}nav{margin:20px 0}nav a{margin-right:16px}.scroll{overflow:auto}</style><main>'''
    document+='<h1>复习来源目录</h1><p>把课程范围、原始文件与教材出处放在一起。<a href="http://127.0.0.1:8765/">打开学习台检索</a></p><p class="notice">本页仅供本机复习，含私人文件链接，不能上传公开仓库。学校正式大纲、旧课表、教师目标与教材复习纲要分别标注；目录节点不代表考试权重。</p>'
    document+='<p>'+str(len(catalog['sources']))+' 个来源 · '+str(len(catalog['relations']))+' 条来源关系 · '+str(len(catalog['issues']))+' 项完整性异常 · 核对 '+esc(catalog['observed_at'])+'</p><nav><a href="#diagnostics">复习专题</a><a href="#courses">逐科范围与缺项</a><a href="#sources">原件与哈希</a></nav>'
    document+='<h2 id="diagnostics">据已存来源整理的复习纲要</h2><p>这里给出可回查的专题和原页，不冒称学校正式大纲，也不以检索命中推断已经掌握。</p><div class="grid">'+''.join(cards)+'</div><h2 id="courses">逐科范围与缺项</h2>'+''.join(courses)+'<h2 id="sources">原件与哈希</h2><div class="scroll"><table><thead><tr><th>来源</th><th>资料类型</th><th>版本说明</th><th>SHA-256 / source_id</th></tr></thead><tbody>'+''.join(assets)+'</tbody></table></div></main></html>'
    Path(output).write_text(document,encoding='utf-8')