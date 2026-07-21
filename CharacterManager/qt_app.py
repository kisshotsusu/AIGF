from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QSize, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSlider, QSplitter, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget,
)

from service import CharacterService, CharacterServiceError


ACCENT = "#22a38a"
STYLE = """
QWidget { color:#17211f; font-family:'Microsoft YaHei UI'; font-size:13px; }
QMainWindow, QWidget#root { background:#eef2f1; }
QFrame#sidebar { background:#183b37; border:none; }
QLabel#brand { color:white; font-size:20px; font-weight:700; }
QLabel#subtitle { color:#d1e7e2; }
QPushButton#nav { color:#e4f2ef; background:transparent; border:0; border-radius:9px; padding:11px 14px; text-align:left; }
QPushButton#nav:hover { background:#27514b; color:white; }
QPushButton#nav:checked { background:#22a38a; color:white; font-weight:600; }
QFrame#card, QGroupBox { background:#ffffff; border:1px solid #b9c9c5; border-radius:12px; }
QGroupBox { font-weight:700; padding:18px 12px 12px 12px; margin-top:10px; }
QGroupBox::title { subcontrol-origin:margin; left:14px; padding:0 6px; color:#173d36; }
QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QListWidget, QTableWidget { color:#101715; background:#ffffff; border:1px solid #9fb2ad; border-radius:7px; padding:6px; selection-background-color:#087f6b; selection-color:#ffffff; }
QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border:1px solid #22a38a; }
QPushButton { background:white; border:1px solid #c8d8d4; border-radius:7px; padding:7px 13px; }
QPushButton:hover { background:#edf7f4; border-color:#8fc8bb; }
QPushButton#primary { background:#22a38a; border-color:#22a38a; color:white; font-weight:600; }
QPushButton#danger { color:#b84343; }
QListWidget::item { color:#17211f; background:#ffffff; padding:8px; border-radius:5px; }
QListWidget::item:selected { color:#ffffff; background:#087f6b; }
QHeaderView::section { color:#10231f; background:#dce7e4; border:0; border-right:1px solid #bac9c5; padding:8px; font-weight:700; }
QTableWidget { color:#111917; background:#ffffff; alternate-background-color:#e8f1ef; gridline-color:#c8d4d1; }
QTableWidget::item:selected { color:#ffffff; background:#087f6b; }
QCheckBox { color:#17211f; spacing:7px; }
QStatusBar { background:white; color:#526963; }
"""


def button(text, slot=None, primary=False, danger=False):
    obj = QPushButton(text)
    if primary: obj.setObjectName("primary")
    if danger: obj.setObjectName("danger")
    if slot: obj.clicked.connect(slot)
    return obj


class JsonSection(QWidget):
    """Lossless advanced editor for a configuration section."""
    def __init__(self, service, title, description, section, home=False):
        super().__init__(); self.service = service; self.section = section; self.home = home
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 22, 24, 22); layout.setSpacing(10)
        layout.addWidget(page_title(title, description))
        self.editor = QTextEdit(); self.editor.setFont(QFont("Cascadia Mono", 10)); layout.addWidget(self.editor, 1)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(button("重新载入", self.load)); row.addWidget(button("保存设置", self.save, primary=True)); layout.addLayout(row)
        self.load()

    def load(self):
        try: self.editor.setPlainText(json.dumps(self.service.get_config_section(self.section, self.home), ensure_ascii=False, indent=2))
        except Exception as exc: alert(self, exc)

    def save(self):
        try:
            value = json.loads(self.editor.toPlainText() or "{}")
            if not isinstance(value, dict): raise ValueError("配置必须是 JSON 对象")
            self.service.save_config_section(self.section, value, self.home)
            toast(self, "设置已安全保存")
        except Exception as exc: alert(self, exc)


class MultiJsonPage(QWidget):
    def __init__(self, service, title, description, sections):
        super().__init__(); self.service=service; self.sections=sections
        lay=QVBoxLayout(self); lay.setContentsMargins(24,22,24,22); lay.addWidget(page_title(title,description))
        bar=QHBoxLayout(); bar.addWidget(QLabel("配置模块")); self.selector=QComboBox(); self.selector.addItems(list(sections)); self.selector.currentTextChanged.connect(self.load); bar.addWidget(self.selector,1); bar.addWidget(button("重新载入",self.load)); lay.addLayout(bar)
        self.editor=QTextEdit(); self.editor.setFont(QFont("Cascadia Mono",10)); lay.addWidget(self.editor,1)
        row=QHBoxLayout(); row.addStretch(); row.addWidget(button("保存当前模块",self.save,primary=True)); lay.addLayout(row); self.load()
    def target(self): return self.sections[self.selector.currentText()]
    def load(self):
        try:
            section,home=self.target(); self.editor.setPlainText(json.dumps(self.service.get_config_section(section,home),ensure_ascii=False,indent=2))
        except Exception as exc: alert(self,exc)
    def save(self):
        try:
            value=json.loads(self.editor.toPlainText() or "{}")
            if not isinstance(value,dict): raise ValueError("配置必须是 JSON 对象")
            section,home=self.target(); self.service.save_config_section(section,value,home); toast(self,f"{self.selector.currentText()} 已保存")
        except Exception as exc: alert(self,exc)


def page_title(title, description=""):
    box = QWidget(); lay = QVBoxLayout(box); lay.setContentsMargins(0, 0, 0, 8); lay.setSpacing(3)
    label = QLabel(title); label.setStyleSheet("font-size:22px;font-weight:700;color:#193b36")
    lay.addWidget(label)
    if description:
        sub = QLabel(description); sub.setWordWrap(True); sub.setStyleSheet("color:#344d47;font-weight:500"); lay.addWidget(sub)
    return box


def alert(parent, exc): QMessageBox.critical(parent, "操作失败", str(exc))
def toast(parent, message):
    window = parent.window()
    if isinstance(window, QMainWindow): window.statusBar().showMessage(message, 4000)


class IdentityPage(QWidget):
    SPECS = [("name","角色名称"),("identity","角色身份"),("gender","性别设定"),("visual_age","视觉年龄"),("personality","核心性格"),("relationship_to_user","与用户关系"),("user_title","对用户称呼")]
    def __init__(self, service):
        super().__init__(); self.service=service; self.fields={}
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); outer.addWidget(scroll)
        body=QWidget(); lay=QVBoxLayout(body); lay.setContentsMargins(24,22,24,22); lay.addWidget(page_title("角色身份", "家庭模式与直播模式共享的角色资料。"))
        card=QFrame(); card.setObjectName("card"); form=QFormLayout(card); form.setContentsMargins(20,18,20,18); form.setSpacing(11)
        for key,label in self.SPECS: self.fields[key]=QLineEdit(); form.addRow(label,self.fields[key])
        for key,label in (("user_name","用户姓名"),("user_aliases","用户别名（逗号分隔）"),("live_usernames","直播用户名（逗号分隔）")):
            self.fields[key]=QLineEdit(); form.addRow(label,self.fields[key])
        self.notes=QTextEdit(); self.notes.setMaximumHeight(100); form.addRow("补充说明",self.notes)
        lay.addWidget(card); row=QHBoxLayout(); row.addStretch(); row.addWidget(button("保存身份",self.save,primary=True)); lay.addLayout(row); lay.addStretch(); scroll.setWidget(body); self.load()
    def load(self):
        d=self.service.load_identity(); char=d.get("character",{}); user=d.get("user",{})
        for key,_ in self.SPECS: self.fields[key].setText(str(char.get(key,"")))
        self.fields["user_name"].setText(str(user.get("name",""))); self.fields["user_aliases"].setText("，".join(user.get("aliases",[]))); self.fields["live_usernames"].setText("，".join(user.get("live_usernames",[])))
        self.notes.setPlainText(str(d.get("notes","")))
    def save(self):
        try:
            split=lambda x:[v.strip() for v in x.replace("，",",").split(",") if v.strip()]
            data={"character":{k:self.fields[k].text().strip() for k,_ in self.SPECS},"user":{"id":"owner","name":self.fields["user_name"].text().strip(),"aliases":split(self.fields["user_aliases"].text()),"live_usernames":split(self.fields["live_usernames"].text())},"notes":self.notes.toPlainText().strip()}
            self.service.save_identity(data); toast(self,"角色身份已保存")
        except Exception as exc: alert(self,exc)


class MemoryDialog(QDialog):
    def __init__(self,parent,item=None):
        super().__init__(parent); self.setWindowTitle("编辑记忆" if item else "添加记忆"); self.resize(620,480); self.item=item or {}
        form=QFormLayout(self); self.kind=QComboBox(); self.kind.addItems(["manual","identity","preference","relationship","agreement","event"]); self.kind.setCurrentText(str(self.item.get("type","manual")))
        self.user=QLineEdit(str(self.item.get("user",""))); self.content=QTextEdit(str(self.item.get("content") or self.item.get("message") or "")); self.tags=QLineEdit("，".join(self.item.get("tags",[]))); self.private=QCheckBox("私密记忆（直播模式不可读取）"); self.private.setChecked(self.item.get("privacy")=="private")
        form.addRow("类型",self.kind); form.addRow("相关用户",self.user); form.addRow("内容",self.content); form.addRow("标签",self.tags); form.addRow("",self.private)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); form.addRow(buttons)
    def value(self):
        tags=[x.strip() for x in self.tags.text().replace("，",",").split(",") if x.strip()]
        return {"type":self.kind.currentText(),"user":self.user.text().strip(),"content":self.content.toPlainText().strip(),"tags":tags,"privacy":"private" if self.private.isChecked() else "shared","attachments":self.item.get("attachments",[])}


class MemoryPage(QWidget):
    def __init__(self,service):
        super().__init__(); self.service=service; self.rows=[]
        lay=QVBoxLayout(self); lay.setContentsMargins(24,22,24,22); lay.addWidget(page_title("长期记忆", "搜索、添加和维护家庭与直播共享的长期记忆。"))
        rules=QGroupBox("记忆写入规则"); grid=QFormLayout(rules); self.rule={}
        top=QWidget(); top_l=QHBoxLayout(top); top_l.setContentsMargins(0,0,0,0)
        self.rule["mode"]=QComboBox(); self.rule["mode"].addItem("仅写入重要内容","important"); self.rule["mode"].addItem("关闭自动写入","off"); self.rule["mode"].addItem("写入全部对话","all")
        self.rule["threshold"]=QSlider(Qt.Horizontal); self.rule["threshold"].setRange(0,100); self.threshold_text=QLabel(); self.rule["threshold"].valueChanged.connect(lambda v:self.threshold_text.setText(str(v)))
        self.threshold_preview=QLabel(); self.threshold_preview.setMinimumWidth(118); self.threshold_preview.setStyleSheet("color:#0b6556;font-weight:700")
        def threshold_changed(v):
            self.threshold_text.setText(str(v)); self.threshold_preview.setText("宽松：写入较多" if v<40 else "均衡：保留重点" if v<75 else "严格：仅高价值")
        self.rule["threshold"].valueChanged.connect(threshold_changed)
        self.rule["daily"]=QSpinBox(); self.rule["daily"].setRange(0,500); self.rule["minimum"]=QSpinBox(); self.rule["minimum"].setRange(1,200); self.rule["ai"]=QCheckBox("使用 AI 判断重要度")
        for label,w in (("写入模式",self.rule["mode"]),("重要度",self.rule["threshold"]),("",self.threshold_text),("",self.threshold_preview),("",self.rule["ai"])): top_l.addWidget(QLabel(label)) if label else None; top_l.addWidget(w)
        limits=QWidget(); limits_l=QHBoxLayout(limits); limits_l.setContentsMargins(0,0,0,0); limits_l.addWidget(QLabel("每日上限")); limits_l.addWidget(self.rule["daily"]); limits_l.addSpacing(20); limits_l.addWidget(QLabel("最短消息长度")); limits_l.addWidget(self.rule["minimum"]); limits_l.addStretch()
        grid.addRow(top); grid.addRow(limits); self.rule["always"]=QLineEdit(); self.rule["ignore"]=QLineEdit(); grid.addRow("强制记忆关键词",self.rule["always"]); grid.addRow("忽略关键词",self.rule["ignore"])
        save_rules=button("保存写入规则",self.save_rules,primary=True); grid.addRow("",save_rules); lay.addWidget(rules)
        bar=QHBoxLayout(); self.search=QLineEdit(); self.search.setPlaceholderText("搜索内容、用户或标签…"); self.search.returnPressed.connect(self.refresh); bar.addWidget(self.search,1); bar.addWidget(button("搜索",self.refresh)); bar.addWidget(button("添加记忆",self.add,primary=True)); lay.addLayout(bar)
        self.table=QTableWidget(0,6); self.table.setAlternatingRowColors(True); self.table.setHorizontalHeaderLabels(["时间","范围","类型","用户","内容","标签"]); self.table.setSelectionBehavior(QTableWidget.SelectRows); self.table.setEditTriggers(QTableWidget.NoEditTriggers); self.table.verticalHeader().hide(); self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch); self.table.doubleClicked.connect(self.edit); lay.addWidget(self.table,1)
        row=QHBoxLayout(); self.count=QLabel(); row.addWidget(self.count); row.addStretch(); row.addWidget(button("编辑",self.edit)); row.addWidget(button("删除",self.delete,danger=True)); lay.addLayout(row); self.load_rules(); self.refresh()
    def load_rules(self):
        c=self.service.get_config_section("memory_write"); mode=str(c.get("mode","important")); i=self.rule["mode"].findData(mode); self.rule["mode"].setCurrentIndex(max(0,i)); self.rule["threshold"].setValue(int(c.get("importance_threshold",70))); self.rule["daily"].setValue(int(c.get("max_daily_writes",20))); self.rule["minimum"].setValue(int(c.get("min_message_length",4))); self.rule["ai"].setChecked(bool(c.get("analyze_with_llm",True))); self.rule["always"].setText("，".join(c.get("always_keywords",[]))); self.rule["ignore"].setText("，".join(c.get("ignore_keywords",[])))
    def save_rules(self):
        try:
            split=lambda s:[x.strip() for x in s.replace("，",",").split(",") if x.strip()]
            value={"mode":self.rule["mode"].currentData(),"importance_threshold":self.rule["threshold"].value(),"max_daily_writes":self.rule["daily"].value(),"min_message_length":self.rule["minimum"].value(),"analyze_with_llm":self.rule["ai"].isChecked(),"always_keywords":split(self.rule["always"].text()),"ignore_keywords":split(self.rule["ignore"].text())}
            self.service.save_config_section("memory_write",value); toast(self,"记忆写入规则已保存")
        except Exception as exc: alert(self,exc)
    def current(self):
        r=self.table.currentRow(); return self.rows[r] if 0<=r<len(self.rows) else None
    def refresh(self):
        try:
            self.rows=self.service.list_memories(self.search.text()); self.table.setRowCount(len(self.rows))
            for r,x in enumerate(self.rows):
                vals=[str(x.get("time","")).replace("T"," "),"私密" if x.get("privacy")=="private" else "共享",str(x.get("type","memory")),str(x.get("user","")),str(x.get("content") or x.get("message") or "")," · ".join(x.get("tags",[]) if isinstance(x.get("tags"),list) else [])]
                for c,v in enumerate(vals): self.table.setItem(r,c,QTableWidgetItem(v))
            self.count.setText(f"{len(self.rows)} 条记忆")
        except Exception as exc: alert(self,exc)
    def add(self): self._dialog(None)
    def edit(self):
        if self.current(): self._dialog(self.current())
    def _dialog(self,item):
        dlg=MemoryDialog(self,item)
        if dlg.exec()==QDialog.Accepted:
            value=dlg.value()
            if not value["content"]: return alert(self,"记忆内容不能为空")
            try: self.service.save_memory(value,item); self.refresh(); toast(self,"记忆已保存")
            except Exception as exc: alert(self,exc)
    def delete(self):
        item=self.current()
        if item and QMessageBox.question(self,"确认删除","确定删除所选记忆吗？")==QMessageBox.Yes:
            try: self.service.delete_memory(item); self.refresh(); toast(self,"记忆已删除")
            except Exception as exc: alert(self,exc)


class DocumentsPage(QWidget):
    def __init__(self,service):
        super().__init__(); self.service=service; self.active=None; self.loading=False; self.timer=QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.save)
        lay=QVBoxLayout(self); lay.setContentsMargins(24,22,24,22); lay.addWidget(page_title("人格规则", "规则文档会在停止输入 800 毫秒后自动保存。"))
        split=QSplitter(); self.list=QListWidget(); self.list.setMaximumWidth(210); self.list.addItems(service.list_documents()); self.list.currentTextChanged.connect(self.select)
        right=QWidget(); rl=QVBoxLayout(right); rl.setContentsMargins(12,0,0,0); self.title=QLabel(); self.title.setStyleSheet("font-size:17px;font-weight:600"); self.editor=QTextEdit(); self.editor.textChanged.connect(lambda: None if self.loading else self.timer.start(800)); rl.addWidget(self.title); rl.addWidget(self.editor,1)
        split.addWidget(self.list); split.addWidget(right); split.setStretchFactor(1,1); lay.addWidget(split,1); self.list.setCurrentRow(0)
    def select(self,name):
        if not name:return
        if self.active and self.timer.isActive(): self.save()
        try: self.loading=True; self.active=name; self.title.setText(name); self.editor.setPlainText(self.service.load_document(name)); self.loading=False
        except Exception as exc: self.loading=False; alert(self,exc)
    def save(self):
        if self.active:
            try: self.service.save_document(self.active,self.editor.toPlainText()); toast(self,f"{self.active} 已自动保存")
            except Exception as exc: alert(self,exc)


class ImagesPage(QWidget):
    def __init__(self,service):
        super().__init__(); self.service=service; self.images=[]; self.primary=None
        lay=QVBoxLayout(self); lay.setContentsMargins(24,22,24,22); lay.addWidget(page_title("角色形象", "管理角色图片并设置默认形象。"))
        split=QSplitter(); left=QWidget(); ll=QVBoxLayout(left); self.list=QListWidget(); self.list.currentRowChanged.connect(self.show_image); ll.addWidget(self.list); row=QHBoxLayout(); row.addWidget(button("添加",self.add,primary=True)); row.addWidget(button("删除",self.delete,danger=True)); ll.addLayout(row)
        right=QWidget(); rl=QVBoxLayout(right); self.preview=QLabel("请选择图片"); self.preview.setAlignment(Qt.AlignCenter); self.preview.setMinimumSize(400,350); self.preview.setStyleSheet("background:white;border:1px solid #dce7e4;border-radius:12px;color:#71837f"); self.info=QLabel(); self.info.setWordWrap(True); rl.addWidget(self.preview,1); rl.addWidget(self.info); rl.addWidget(button("设为默认形象",self.set_primary,primary=True),0,Qt.AlignRight)
        split.addWidget(left); split.addWidget(right); split.setStretchFactor(1,1); lay.addWidget(split,1)
        appearance=QGroupBox("固定外观文档（CHARACTER.md）"); appearance_l=QVBoxLayout(appearance); self.appearance_editor=QTextEdit(); self.appearance_editor.setMinimumHeight(120); self.appearance_editor.setMaximumHeight(190); appearance_l.addWidget(self.appearance_editor); appearance_l.addWidget(button("保存固定外观文档",self.save_appearance,primary=True),0,Qt.AlignRight); lay.addWidget(appearance)
        self.appearance_editor.setPlainText(self.service.load_appearance_profile()); self.refresh()
    def save_appearance(self):
        try:self.service.save_appearance_profile(self.appearance_editor.toPlainText());toast(self,"固定外观文档已保存")
        except Exception as exc:alert(self,exc)
    def current(self):
        i=self.list.currentRow(); return self.images[i] if 0<=i<len(self.images) else None
    def refresh(self,select=None):
        self.primary,self.images=self.service.list_images(); self.list.clear(); selected=0
        for i,x in enumerate(self.images):
            self.list.addItem(("★ " if x.get("id")==self.primary else "   ")+str(x.get("label") or x.get("filename")))
            if x.get("id")==select:selected=i
        if self.images:self.list.setCurrentRow(selected)
    def show_image(self,_=None):
        x=self.current()
        if not x:self.preview.setText("请选择图片");self.preview.setPixmap(QPixmap());return
        path=self.service.image_path(x); pix=QPixmap(str(path)); self.preview.setPixmap(pix.scaled(self.preview.size()-QSize(24,24),Qt.KeepAspectRatio,Qt.SmoothTransformation)) if not pix.isNull() else self.preview.setText("图片无法预览")
        self.info.setText(f"名称：{x.get('label','')}\n文件：{x.get('filename','')}\n标签：{' · '.join(x.get('tags',[]))}")
    def add(self):
        path,_=QFileDialog.getOpenFileName(self,"选择图片","","图片 (*.png *.jpg *.jpeg *.webp *.gif)")
        if path:
            try: image_id=self.service.add_image(Path(path));self.refresh(image_id);toast(self,"图片已添加")
            except Exception as exc:alert(self,exc)
    def set_primary(self):
        if self.current():
            try:self.service.set_primary_image(self.current()["id"]);self.refresh(self.current()["id"]);toast(self,"默认形象已更新")
            except Exception as exc:alert(self,exc)
    def delete(self):
        x=self.current()
        if x and QMessageBox.question(self,"确认删除","确定删除所选图片吗？")==QMessageBox.Yes:
            try:self.service.delete_image(x["id"]);self.refresh();toast(self,"图片已删除")
            except Exception as exc:alert(self,exc)


class ModelPage(QWidget):
    def __init__(self,s):
        super().__init__(); self.s=s; self.providers={}; lay=QVBoxLayout(self);lay.setContentsMargins(24,22,24,22);lay.addWidget(page_title("模型 API","可视化配置供应商、模型密钥以及不同场景的生成参数。"))
        general=QGroupBox("场景参数"); f=QFormLayout(general); self.current=QComboBox(); self.current.addItems(["deepseek","mimo","custom"]); f.addRow("当前供应商",self.current); profiles=QWidget(); pl=QHBoxLayout(profiles);pl.setContentsMargins(0,0,0,0);self.tuning={}
        for key,label in (("home","家庭对话"),("live","直播回复"),("memory","记忆判断")):
            box=QGroupBox(label);bf=QFormLayout(box);temp=QDoubleSpinBox();temp.setRange(0,2);temp.setSingleStep(.05);temp.setDecimals(2);tokens=QSpinBox();tokens.setRange(1,32000);bf.addRow("温度",temp);bf.addRow("最大 Tokens",tokens);pl.addWidget(box);self.tuning[key]=(temp,tokens)
        f.addRow(profiles);lay.addWidget(general)
        tabs=QTabWidget(); self.provider_tabs=tabs
        for name,label in (("deepseek","DeepSeek"),("mimo","小米 MiMo"),("custom","自定义")):
            w=QWidget();wf=QFormLayout(w);base=QLineEdit();model=QLineEdit();env=QLineEdit();secret=QLineEdit();secret.setEchoMode(QLineEdit.Password);secret.setPlaceholderText("留空则保留现有密钥");status=QLabel();status.setStyleSheet("font-weight:700");wf.addRow("Base URL",base);wf.addRow("模型名称",model);wf.addRow("密钥环境变量",env);wf.addRow("API Key",secret);wf.addRow("当前状态",status);tabs.addTab(w,label);self.providers[name]=(base,model,env,secret,status)
        tabs.addTab(MiMoMultimodalPage(s, embedded=True),"MiMo 多模态")
        lay.addWidget(tabs,1);row=QHBoxLayout();row.addStretch();row.addWidget(button("保存模型 API",self.save,primary=True));lay.addLayout(row);self.load()
    def load(self):
        c=self.s.get_config_section("llm");self.current.setCurrentText(str(c.get("provider","deepseek")));defaults={"home":(.7,600),"live":(.55,160),"memory":(.2,180)}
        for k,(a,b) in self.tuning.items():v=c.get(k,{});a.setValue(float(v.get("temperature",defaults[k][0])));b.setValue(int(v.get("max_tokens",defaults[k][1])))
        ps=c.get("providers",{})
        secrets=self.s.read_env()
        for name,(base,model,env,secret,status) in self.providers.items():
            v=ps.get(name,{});base.setText(str(v.get("base_url","")));model.setText(str(v.get("model","")));key_name=str(v.get("api_key_env",name.upper()+"_API_KEY"));env.setText(key_name);secret.clear();configured=bool(secrets.get(key_name));status.setText("● 已配置现有 API Key" if configured else "○ 尚未配置 API Key");status.setStyleSheet("color:#08745f;font-weight:700" if configured else "color:#a12d2d;font-weight:700")
    def save(self):
        try:
            old=self.s.get_config_section("llm");old["provider"]=self.current.currentText();old.setdefault("providers",{})
            for k,(a,b) in self.tuning.items():old[k]={"temperature":a.value(),"max_tokens":b.value()}
            for name,(base,model,env,secret,status) in self.providers.items():entry=dict(old["providers"].get(name,{}) or {});entry.update({"base_url":base.text().strip(),"model":model.text().strip(),"api_key_env":env.text().strip()});old["providers"][name]=entry;self.s.save_secret(env.text().strip(),secret.text().strip());secret.clear()
            self.s.save_config_section("llm",old);self.load();toast(self,"模型 API 设置已保存")
        except Exception as exc:alert(self,exc)


class VoicePage(QWidget):
    def __init__(self,s):
        super().__init__();self.s=s;lay=QVBoxLayout(self);lay.setContentsMargins(24,22,24,22);lay.addWidget(page_title("语音服务","可视化配置语音合成、识别、麦克风和识别后自动发送。"));tabs=QTabWidget();lay.addWidget(tabs,1)
        t=QWidget();tf=QFormLayout(t);self.tts={};
        for key,label in (("url","TTS 地址"),("health_url","健康检查地址"),("start_command","启动命令"),("model","模型"),("reference","参考音频"),("speaker","说话人")):
            self.tts[key]=QLineEdit();tf.addRow(label,self.tts[key])
        self.tts_enabled=QCheckBox("启用语音合成");self.tts_auto=QCheckBox("服务未运行时自动启动");self.tts_play=QCheckBox("生成后自动播放");tf.addRow(self.tts_enabled);tf.addRow(self.tts_auto);tf.addRow(self.tts_play);tabs.addTab(t,"语音合成 TTS")
        st=QWidget();sf=QFormLayout(st);self.stt={};self.stt_mode=QComboBox();self.stt_mode.addItems(["sound_mcp","mimo","api","local"]);sf.addRow("识别模式",self.stt_mode)
        for key,label in (("language","识别语言"),("mcp_url","Sound MCP 地址"),("api_url","API 地址"),("model","模型"),("local_python","本地 Python"),("local_model","本地模型")):
            self.stt[key]=QLineEdit();sf.addRow(label,self.stt[key])
        self.stt_auto=QCheckBox("自动启动识别服务");sf.addRow(self.stt_auto);tabs.addTab(st,"语音识别 STT")
        m=QWidget();mf=QFormLayout(m);self.current_device=QLabel();self.current_device.setWordWrap(True);self.current_device.setStyleSheet("color:#123d35;font-weight:700;background:#e7f2ef;border:1px solid #a8c5be;border-radius:7px;padding:9px");self.change_devices=button("更改设备",self.toggle_devices);self.devices=QListWidget();self.devices.setMinimumHeight(210);self.devices.setVisible(False);self.rate=QSpinBox();self.rate.setRange(8000,192000);self.channels=QSpinBox();self.channels.setRange(1,8);self.auto_send=QCheckBox("识别完成后自动发送消息");current_row=QWidget();cr=QHBoxLayout(current_row);cr.setContentsMargins(0,0,0,0);cr.addWidget(self.current_device,1);cr.addWidget(self.change_devices);mf.addRow("当前输入设备",current_row);mf.addRow("选择设备",self.devices);mf.addRow("采样率",self.rate);mf.addRow("声道数",self.channels);mf.addRow(self.auto_send);tabs.addTab(m,"麦克风与发送")
        row=QHBoxLayout();row.addStretch();row.addWidget(button("保存语音设置",self.save,primary=True));lay.addLayout(row);self.load()
    def load(self):
        t=self.s.get_config_section("tts");
        for k,w in self.tts.items():w.setText(str(t.get(k,"")))
        self.tts_enabled.setChecked(bool(t.get("enabled",True)));self.tts_auto.setChecked(bool(t.get("auto_start",True)));self.tts_play.setChecked(bool(t.get("play_audio",True)))
        st=self.s.get_config_section("stt",True);self.stt_mode.setCurrentText(str(st.get("mode","sound_mcp")))
        for k,w in self.stt.items():w.setText(str(st.get(k,"")))
        self.stt_auto.setChecked(bool(st.get("auto_start",True)));m=self.s.get_config_section("microphone",True);self.rate.setValue(int(m.get("sample_rate",16000)));self.channels.setValue(int(m.get("channels",1)));self.auto_send.setChecked(bool(m.get("auto_send_after_transcription",True)));self.load_devices(m)
    def load_devices(self,config):
        self.devices.clear(); selected=set(config.get("device_ids",[config.get("device_id",-1)]))
        try:
            import sounddevice as sd
            for index,dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels",0))<=0:continue
                host=sd.query_hostapis(int(dev.get("hostapi",0))).get("name","");item=QListWidgetItem(f"[{index}] {dev.get('name','未知设备')}  ·  {host}  ·  {int(dev.get('max_input_channels',0))} 声道");item.setData(Qt.UserRole,index);item.setFlags(item.flags()|Qt.ItemIsUserCheckable);item.setCheckState(Qt.Checked if index in selected else Qt.Unchecked);self.devices.addItem(item)
        except Exception as exc:
            item=QListWidgetItem(f"无法枚举音频设备：{exc}");item.setFlags(Qt.NoItemFlags);self.devices.addItem(item)
        checked=[self.devices.item(i).text() for i in range(self.devices.count()) if self.devices.item(i).flags()&Qt.ItemIsUserCheckable and self.devices.item(i).checkState()==Qt.Checked]
        self.current_device.setText("\n".join(checked) if checked else "未选择输入设备（使用系统默认设备）")
    def toggle_devices(self):
        visible=not self.devices.isVisible();self.devices.setVisible(visible);self.change_devices.setText("收起设备列表" if visible else "更改设备")
    def save(self):
        try:
            t=self.s.get_config_section("tts");t.update({k:w.text().strip() for k,w in self.tts.items()});t.update(enabled=self.tts_enabled.isChecked(),auto_start=self.tts_auto.isChecked(),play_audio=self.tts_play.isChecked());self.s.save_config_section("tts",t)
            st=self.s.get_config_section("stt",True);st.update({k:w.text().strip() for k,w in self.stt.items()});st["mode"]=self.stt_mode.currentText();st["auto_start"]=self.stt_auto.isChecked();self.s.save_config_section("stt",st,True)
            chosen=[self.devices.item(i).data(Qt.UserRole) for i in range(self.devices.count()) if self.devices.item(i).flags()&Qt.ItemIsUserCheckable and self.devices.item(i).checkState()==Qt.Checked];m=self.s.get_config_section("microphone",True);m.update(device_id=(chosen[0] if chosen else -1),device_ids=chosen,sample_rate=self.rate.value(),channels=self.channels.value(),auto_send_after_transcription=self.auto_send.isChecked());self.s.save_config_section("microphone",m,True);self.load_devices(m);self.devices.setVisible(False);self.change_devices.setText("更改设备");toast(self,"语音和自动发送设置已保存")
        except Exception as exc:alert(self,exc)


class ImageApiPage(QWidget):
    def __init__(self,s):
        super().__init__();self.s=s;lay=QVBoxLayout(self);lay.setContentsMargins(24,22,24,22);lay.addWidget(page_title("图片 API","配置角色图片生成接口，密钥安全保存到项目 .env。"));box=QGroupBox("图像生成服务");f=QFormLayout(box);self.mode=QComboBox();self.mode.addItems(["images","chat_multimodal"]);self.base=QLineEdit();self.model=QLineEdit();self.size=QComboBox();self.size.setEditable(True);self.size.addItems(["1024x1024","1536x1024","1024x1536"]);self.timeout=QSpinBox();self.timeout.setRange(10,1800);self.env=QLineEdit();self.key=QLineEdit();self.key.setEchoMode(QLineEdit.Password);self.key.setPlaceholderText("留空则保留现有密钥")
        for label,w in (("接口模式",self.mode),("Base URL",self.base),("模型名称",self.model),("输出尺寸",self.size),("超时秒数",self.timeout),("密钥环境变量",self.env),("API Key",self.key)):f.addRow(label,w)
        lay.addWidget(box);ubox=QGroupBox("MiMo 图像理解服务");uf=QFormLayout(ubox);self.ubase=QLineEdit();self.umodel=QLineEdit();self.uenv=QLineEdit();self.ukey=QLineEdit();self.ukey.setEchoMode(QLineEdit.Password);self.ukey.setPlaceholderText("留空则保留现有密钥");self.utimeout=QSpinBox();self.utimeout.setRange(10,600)
        for label,w in (("Base URL",self.ubase),("多模态模型",self.umodel),("超时秒数",self.utimeout),("密钥环境变量",self.uenv),("API Key",self.ukey)):uf.addRow(label,w)
        note=QLabel("MiMo 在 chat/completions 中提供图片理解并返回文字，不替代上方的图片生成/编辑服务。");note.setWordWrap(True);uf.addRow(note);lay.addWidget(ubox);lay.addStretch();row=QHBoxLayout();row.addStretch();row.addWidget(button("保存图片 API",self.save,primary=True));lay.addLayout(row);self.load()
    def load(self):
        c=self.s.get_config_section("image_generation");self.mode.setCurrentText(str(c.get("mode","images")));self.base.setText(str(c.get("base_url","")));self.model.setText(str(c.get("model","")));self.size.setCurrentText(str(c.get("size","1024x1024")));self.timeout.setValue(int(c.get("timeout_seconds",180)));self.env.setText(str(c.get("api_key_env","IMAGE_API_KEY")));u=self.s.get_config_section("image_understanding");self.ubase.setText(str(u.get("base_url","https://api.xiaomimimo.com/v1")));self.umodel.setText(str(u.get("model","mimo-v2.5")));self.utimeout.setValue(int(u.get("timeout_seconds",60)));self.uenv.setText(str(u.get("api_key_env","MIMO_API_KEY")))
    def save(self):
        try:self.s.save_config_section("image_generation",{"mode":self.mode.currentText(),"base_url":self.base.text().strip(),"model":self.model.text().strip(),"size":self.size.currentText().strip(),"timeout_seconds":self.timeout.value(),"api_key_env":self.env.text().strip()});self.s.save_secret(self.env.text().strip(),self.key.text().strip());self.s.save_config_section("image_understanding",{"provider":"mimo","base_url":self.ubase.text().strip(),"model":self.umodel.text().strip(),"timeout_seconds":self.utimeout.value(),"api_key_env":self.uenv.text().strip(),"auth_header":"api-key","max_tokens_field":"max_completion_tokens","max_completion_tokens":1024,"extra_body":{"thinking":{"type":"disabled"}}});self.s.save_secret(self.uenv.text().strip(),self.ukey.text().strip());self.key.clear();self.ukey.clear();toast(self,"图片 API 设置已保存")
        except Exception as exc:alert(self,exc)


class MiMoMultimodalPage(QWidget):
    def __init__(self,s,embedded=False):
        super().__init__();self.s=s
        lay=QVBoxLayout(self);lay.setContentsMargins(12,12,12,12);lay.setSpacing(10)
        if not embedded:lay.addWidget(page_title("MiMo 多模态","统一管理任务完成检查、图片理解和语音识别。API Key 仅显示配置状态，不回显明文。"))
        else:
            intro=QLabel("配置 MiMo 的统一接口、图片理解、语音识别和任务完成检查。页面内容可上下滚动，保存按钮始终位于底部。")
            intro.setWordWrap(True);intro.setStyleSheet("color:#294a43;background:#e7f2ef;border:1px solid #b7cec8;border-radius:8px;padding:9px 11px;font-weight:600")
            lay.addWidget(intro)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame);scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded);scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content=QWidget();content.setMinimumWidth(610);body=QVBoxLayout(content);body.setContentsMargins(4,2,8,8);body.setSpacing(12)
        box=QGroupBox("1. 服务与认证");f=QFormLayout(box);f.setLabelAlignment(Qt.AlignRight|Qt.AlignVCenter);f.setHorizontalSpacing(18);f.setVerticalSpacing(10)
        self.enabled=QCheckBox("启用 MiMo 多模态能力");self.base=QLineEdit();self.env=QLineEdit();self.key=QLineEdit();self.key.setEchoMode(QLineEdit.Password);self.key.setPlaceholderText("输入新密钥；留空则保留现有密钥");self.key_status=QLabel();self.timeout=QSpinBox();self.timeout.setRange(10,600);self.timeout.setSuffix(" 秒")
        for w in (self.base,self.env,self.key):w.setMinimumWidth(390)
        for label,w in (("",self.enabled),("Base URL",self.base),("密钥环境变量",self.env),("API Key",self.key),("当前密钥",self.key_status),("请求超时",self.timeout)):f.addRow(label,w)
        body.addWidget(box)
        caps=QGroupBox("2. 图片与语音能力");cf=QFormLayout(caps);cf.setLabelAlignment(Qt.AlignRight|Qt.AlignVCenter);cf.setHorizontalSpacing(18);cf.setVerticalSpacing(10)
        self.image_enabled=QCheckBox("启用图片理解");self.image_model=QLineEdit();self.speech_enabled=QCheckBox("启用语音识别");self.speech_model=QLineEdit();self.language=QComboBox();self.language.addItems(["auto（自动判断）","zh（中文）","en（英文）"])
        self.image_model.setMinimumWidth(390);self.speech_model.setMinimumWidth(390);self.language.setMinimumWidth(190)
        for label,w in (("",self.image_enabled),("图片模型",self.image_model),("",self.speech_enabled),("语音模型",self.speech_model),("默认识别语言",self.language)):cf.addRow(label,w)
        body.addWidget(caps)
        check=QGroupBox("3. 任务完成检查");vf=QFormLayout(check);vf.setLabelAlignment(Qt.AlignRight|Qt.AlignVCenter);vf.setHorizontalSpacing(18);vf.setVerticalSpacing(10)
        self.check_enabled=QCheckBox("执行类任务结束前调用 MiMo 独立核验");self.check_model=QLineEdit();self.check_model.setMinimumWidth(390);self.retries=QSpinBox();self.retries.setRange(0,5);self.retries.setSuffix(" 次");self.fail_closed=QCheckBox("核验接口异常时禁止报告已完成")
        for label,w in (("",self.check_enabled),("核验模型",self.check_model),("失败后重试",self.retries),("",self.fail_closed)):vf.addRow(label,w)
        note=QLabel("核验器只读取本地工具返回的证据。检查失败时，原因会交回 Agent 继续修正；需要使用 MiMo 录音识别时，请在“语音”页面把识别模式改为 mimo。")
        note.setWordWrap(True);note.setStyleSheet("color:#24423c;background:#f0f6f4;border-radius:7px;padding:9px");vf.addRow("说明",note);body.addWidget(check);body.addStretch()
        scroll.setWidget(content);lay.addWidget(scroll,1)
        row=QHBoxLayout();row.addStretch();save_button=button("保存 MiMo 多模态设置",self.save,primary=True);save_button.setMinimumWidth(190);row.addWidget(save_button);lay.addLayout(row);self.load()
    def load(self):
        c=self.s.get_config_section("mimo_multimodal");self.enabled.setChecked(bool(c.get("enabled",True)));self.base.setText(str(c.get("base_url","https://api.xiaomimimo.com/v1")));self.env.setText(str(c.get("api_key_env","MIMO_API_KEY")));self.timeout.setValue(int(c.get("timeout_seconds",60)));self.image_enabled.setChecked(bool(c.get("image_enabled",True)));self.image_model.setText(str(c.get("image_model","mimo-v2.5")));self.speech_enabled.setChecked(bool(c.get("speech_enabled",True)));self.speech_model.setText(str(c.get("speech_model","mimo-v2.5-asr")));lang=str(c.get("speech_language","auto"));self.language.setCurrentIndex({"auto":0,"zh":1,"en":2}.get(lang,0));self.check_enabled.setChecked(bool(c.get("completion_check_enabled",True)));self.check_model.setText(str(c.get("completion_model","mimo-v2.5")));self.retries.setValue(int(c.get("completion_max_retries",2)));self.fail_closed.setChecked(bool(c.get("fail_closed",True)));configured=bool(self.s.read_env().get(self.env.text().strip()));self.key_status.setText("● 已配置（内容已隐藏）" if configured else "○ 尚未配置");self.key_status.setStyleSheet("color:#08745f;font-weight:700" if configured else "color:#a12d2d;font-weight:700")
    def save(self):
        try:
            c=self.s.get_config_section("mimo_multimodal");c.update(enabled=self.enabled.isChecked(),base_url=self.base.text().strip(),api_key_env=self.env.text().strip(),image_enabled=self.image_enabled.isChecked(),image_model=self.image_model.text().strip(),speech_enabled=self.speech_enabled.isChecked(),speech_model=self.speech_model.text().strip(),speech_language=("auto","zh","en")[self.language.currentIndex()],completion_check_enabled=self.check_enabled.isChecked(),completion_model=self.check_model.text().strip(),completion_max_retries=self.retries.value(),timeout_seconds=self.timeout.value(),max_completion_tokens=1024,fail_closed=self.fail_closed.isChecked());self.s.save_config_section("mimo_multimodal",c);self.s.save_secret(self.env.text().strip(),self.key.text().strip());self.key.clear();self.load();toast(self,"MiMo 多模态设置已保存")
        except Exception as exc:alert(self,exc)


class ToolsPage(QWidget):
    def __init__(self,s):
        super().__init__();self.s=s;lay=QVBoxLayout(self);lay.setContentsMargins(24,22,24,22);lay.addWidget(page_title("工具维护","可视化维护操作权限、Vision、软件映射、MCP 和上下文清理。"));tabs=QTabWidget();lay.addWidget(tabs,1)
        control=QWidget();cf=QFormLayout(control);self.control_checks={}
        for key,label in (("enabled","启用电脑控制"),("full_access","允许完整电脑操作"),("confirm_before_action","执行操作前请求确认"),("confirm_launch_app","打开程序前请求确认")):
            w=QCheckBox(label);self.control_checks[key]=w;cf.addRow(w)
        self.apps=QTableWidget(0,2);self.apps.setHorizontalHeaderLabels(["软件名称","程序或目录路径"]);self.apps.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch);cf.addRow("软件映射",self.apps);ar=QHBoxLayout();ar.addWidget(button("选择程序",self.choose_program));ar.addWidget(button("选择目录",self.choose_directory));ar.addWidget(button("添加空行",lambda:self.apps.insertRow(self.apps.rowCount())));ar.addWidget(button("删除选中",lambda:self.apps.removeRow(self.apps.currentRow()) if self.apps.currentRow()>=0 else None,danger=True));cf.addRow(ar);tabs.addTab(control,"电脑控制")
        vision=QWidget();vf=QFormLayout(vision);self.vision_checks={}
        for key,label in (("enabled","启用 Vision MCP"),("auto_start","自动启动 Vision 服务"),("gui_enabled","启用 GUI-Actor 图像识别"),("preload_model","启动时预加载模型")):
            w=QCheckBox(label);self.vision_checks[key]=w;vf.addRow(w)
        self.vision_host=QLineEdit();self.vision_port=QSpinBox();self.vision_port.setRange(1,65535);self.vision_timeout=QSpinBox();self.vision_timeout.setRange(10,600);vf.addRow("服务地址",self.vision_host);vf.addRow("端口",self.vision_port);vf.addRow("启动超时秒数",self.vision_timeout);note=QLabel("关闭 GUI 图像识别可减少显存占用；保存设置不会在此页面直接加载模型。");note.setWordWrap(True);note.setStyleSheet("color:#253b36;font-weight:600");vf.addRow(note);tabs.addTab(vision,"Vision")
        mcp=QWidget();ml=QVBoxLayout(mcp);self.mcp=QTableWidget(0,3);self.mcp.setHorizontalHeaderLabels(["名称","类型（http/stdio）","地址或命令"]);self.mcp.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch);ml.addWidget(self.mcp);mr=QHBoxLayout();mr.addWidget(button("添加 MCP",lambda:self.mcp.insertRow(self.mcp.rowCount()),primary=True));mr.addWidget(button("删除选中",lambda:self.mcp.removeRow(self.mcp.currentRow()) if self.mcp.currentRow()>=0 else None,danger=True));mr.addStretch();ml.addLayout(mr);tabs.addTab(mcp,"MCP 服务")
        maint=QWidget();mf=QFormLayout(maint);self.home_clean=QCheckBox("启用家庭上下文每日压缩");self.home_time=QLineEdit();self.live_clean=QCheckBox("启用直播上下文过期清理");self.live_minutes=QSpinBox();self.live_minutes.setRange(10,1440);mf.addRow(self.home_clean);mf.addRow("家庭压缩时间",self.home_time);mf.addRow(self.live_clean);mf.addRow("直播保留分钟",self.live_minutes);tabs.addTab(maint,"上下文维护")
        row=QHBoxLayout();row.addStretch();row.addWidget(button("保存工具维护设置",self.save,primary=True));lay.addLayout(row);self.load()
    def put_application(self,path):
        if not path:return
        row=self.apps.currentRow()
        if row<0:row=self.apps.rowCount();self.apps.insertRow(row)
        name=Path(path).stem if Path(path).is_file() else Path(path).name
        if not self.apps.item(row,0):self.apps.setItem(row,0,QTableWidgetItem(name))
        self.apps.setItem(row,1,QTableWidgetItem(path));self.apps.setCurrentCell(row,0)
    def choose_program(self):
        path,_=QFileDialog.getOpenFileName(self,"选择可执行程序","","程序 (*.exe *.bat *.cmd *.lnk);;所有文件 (*.*)");self.put_application(path)
    def choose_directory(self):
        self.put_application(QFileDialog.getExistingDirectory(self,"选择软件目录"))
    def load(self):
        c=self.s.get_config_section("computer_control",True)
        for k,w in self.control_checks.items():w.setChecked(bool(c.get(k,False)))
        apps=c.get("applications",{});self.apps.setRowCount(len(apps))
        for r,(n,p) in enumerate(apps.items()):self.apps.setItem(r,0,QTableWidgetItem(str(n)));self.apps.setItem(r,1,QTableWidgetItem(str(p)))
        v=self.s.get_config_section("vision_mcp",True)
        for k,w in self.vision_checks.items():w.setChecked(bool(v.get(k,False)))
        self.vision_host.setText(str(v.get("host","127.0.0.1")));self.vision_port.setValue(int(v.get("port",8765)));self.vision_timeout.setValue(int(v.get("startup_timeout_seconds",120)))
        servers=self.s.load_mcp_servers();self.mcp.setRowCount(len(servers))
        for r,(name,item) in enumerate(servers.items()):
            kind="http" if item.get("url") else "stdio";target=str(item.get("url") or " ".join([str(item.get("command","")),*map(str,item.get("args",[]))]).strip())
            for col,val in enumerate((name,kind,target)):self.mcp.setItem(r,col,QTableWidgetItem(val))
        h=self.s.get_config_section("context_maintenance",True);l=self.s.get_config_section("context_cleanup");self.home_clean.setChecked(bool(h.get("enabled",True)));self.home_time.setText(str(h.get("time","03:00")));self.live_clean.setChecked(bool(l.get("live_enabled",True)));self.live_minutes.setValue(int(l.get("live_max_age_minutes",120)))
    def save(self):
        try:
            c=self.s.get_config_section("computer_control",True)
            for k,w in self.control_checks.items():c[k]=w.isChecked()
            c["applications"]={self.apps.item(r,0).text().strip():self.apps.item(r,1).text().strip() for r in range(self.apps.rowCount()) if self.apps.item(r,0) and self.apps.item(r,1) and self.apps.item(r,0).text().strip()};self.s.save_config_section("computer_control",c,True)
            v=self.s.get_config_section("vision_mcp",True)
            for k,w in self.vision_checks.items():v[k]=w.isChecked()
            v.update(host=self.vision_host.text().strip(),port=self.vision_port.value(),startup_timeout_seconds=self.vision_timeout.value());self.s.save_config_section("vision_mcp",v,True)
            servers={}
            for r in range(self.mcp.rowCount()):
                if not self.mcp.item(r,0) or not self.mcp.item(r,2):continue
                name=self.mcp.item(r,0).text().strip();kind=self.mcp.item(r,1).text().strip().lower() if self.mcp.item(r,1) else "http";target=self.mcp.item(r,2).text().strip()
                if name:servers[name]={"url":target,"disabled":False} if kind=="http" else {"command":target.split()[0] if target.split() else "","args":target.split()[1:],"disabled":False}
            self.s.save_mcp_servers(servers);h=self.s.get_config_section("context_maintenance",True);h.update(enabled=self.home_clean.isChecked(),time=self.home_time.text().strip() or "03:00");self.s.save_config_section("context_maintenance",h,True);l=self.s.get_config_section("context_cleanup");l.update(live_enabled=self.live_clean.isChecked(),live_max_age_minutes=self.live_minutes.value(),check_interval_seconds=60);self.s.save_config_section("context_cleanup",l);toast(self,"工具维护设置已保存")
        except Exception as exc:alert(self,exc)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.service=CharacterService(); self.setWindowTitle("AI 角色管理器"); self.resize(1180,780); self.setMinimumSize(940,650)
        root=QWidget(); root.setObjectName("root"); self.setCentralWidget(root); main=QHBoxLayout(root); main.setContentsMargins(0,0,0,0); main.setSpacing(0)
        side=QFrame(); side.setObjectName("sidebar"); side.setFixedWidth(210); sl=QVBoxLayout(side); sl.setContentsMargins(17,23,17,20); brand=QLabel("角色管理器"); brand.setObjectName("brand"); sub=QLabel("Character Studio"); sub.setObjectName("subtitle"); sl.addWidget(brand);sl.addWidget(sub);sl.addSpacing(20)
        self.stack=QStackedWidget(); pages=[IdentityPage(self.service),MemoryPage(self.service),DocumentsPage(self.service),ModelPage(self.service),VoicePage(self.service),ToolsPage(self.service),ImagesPage(self.service),ImageApiPage(self.service)]
        labels=["身份","记忆","人格规则","模型 API","语音","工具维护","形象","图片 API"]; self.nav=[]
        for i,label in enumerate(labels):
            b=QPushButton(label);b.setObjectName("nav");b.setCheckable(True);b.clicked.connect(lambda checked,n=i:self.switch(n));sl.addWidget(b);self.nav.append(b)
            self.stack.addWidget(pages[i])
        sl.addStretch(); ver=QLabel("Qt UI · 服务层已分离");ver.setObjectName("subtitle");sl.addWidget(ver);main.addWidget(side);main.addWidget(self.stack,1);self.statusBar().showMessage("就绪");self.switch(0)
    def switch(self,index):
        self.stack.setCurrentIndex(index)
        for i,b in enumerate(self.nav):b.setChecked(i==index)


def run():
    app=QApplication.instance() or QApplication(sys.argv);app.setApplicationName("AI Character Manager");app.setStyle("Fusion");app.setStyleSheet(STYLE)
    lock_path = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation)) / "ai-character-manager.lock"
    lock = QLockFile(str(lock_path)); lock.setStaleLockTime(30000)
    if not lock.tryLock(100):
        # Recover a lock left behind by a crash or forced process termination.
        lock.removeStaleLockFile()
        if not lock.tryLock(100):
            QMessageBox.information(None, "角色管理器", "角色管理器已经在运行。")
            return 0
    app._character_manager_lock = lock
    win=MainWindow();win.show();return app.exec()


if __name__=="__main__": raise SystemExit(run())
