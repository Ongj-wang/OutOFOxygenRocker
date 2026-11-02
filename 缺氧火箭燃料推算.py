# -*- coding: utf-8 -*-
"""
缺氧（Oxygen Not Included）火箭燃料 / 氧化剂用量推算脚本
================================================================
公式来源：《终极版-缺氧火箭载重.xlsx》「终-火箭发射距离计算」工作表。

正向逻辑（与原表一致，用于校验）：
    燃烧效率 eff      = 蒸汽20 / 石油40 / 液氢60            (km/kg)
    引擎质量          = 石油200 / 液氢500 / 蒸汽2000        (kg/个，注：原表为单引擎质量)
    固燃推进器质量    = 选用 ? 1000 : 0
    舱室质量          = 单价 × 数量  (指挥舱200 / 研究舱200 / 货舱2000 / 燃料舱100 / 氧化剂舱100)
                        注：此处 100kg 为燃料舱/氧化剂舱「舱体自重」，非装载容量
    氧化剂提升 boost  = 氧石1.0 / 液氧1.33
    推进距离  thrust  = eff × ( 蒸汽引擎 ? 燃料 : min(燃料, 氧化剂) × boost )
    火箭总质量 total  = 固燃+引擎+指挥舱+研究舱+货舱+燃料舱+氧化剂舱 + 燃料 + 氧化剂
    载重惩罚  penalty = total < 4000 ? total : (total/300) ** 3.2
    最终到达  reach   = thrust - penalty   ( < 0 表示无法到达 )

反向需求（本脚本核心功能）：
    给定「目标里程 target（最终到达距离 km）」+「舱室配置」，反推所需 燃料 / 氧化剂质量。
    燃料同时进入推进项与总质量项，方程非线性；reach(燃料) 先升后降存在最远可达距离，
    在单调上升段用倍增扩界 + 二分求「满足目标的最少燃料量」。
"""

import json
import math
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

# ---------- 基础参数表（取自原表 IF 分支） ----------
ENGINE_EFFICIENCY = {"蒸汽引擎": 20.0, "石油引擎": 40.0, "液氢引擎": 60.0}     # km/kg
ENGINE_MASS       = {"石油引擎": 200.0, "液氢引擎": 500.0, "蒸汽引擎": 2000.0} # kg
OXIDIZER_BOOST    = {"氧石": 1.0, "液氧": 1.33}                                # 燃烧效率提升系数
CABIN_UNIT_MASS   = {"指挥舱": 200.0, "研究舱": 200.0, "货舱": 2000.0,
                     "燃料舱": 100.0, "氧化剂舱": 100.0}
SOLID_BOOSTER_MASS = 1000.0


class RocketConfig:
    """火箭配置。蒸汽引擎不需要氧化剂/氧化剂舱/燃料舱，相应数量请填 0。"""

    def __init__(self, engine, oxidizer="氧石", engine_count=1, use_booster=False,
                 指挥舱=1, 研究舱=0, 货舱=0, 燃料舱=0, 氧化剂舱=0):
        if engine not in ENGINE_EFFICIENCY:
            raise ValueError(f"引擎只能是 {list(ENGINE_EFFICIENCY)}")
        if oxidizer not in OXIDIZER_BOOST and engine != "蒸汽引擎":
            raise ValueError(f"氧化剂只能是 {list(OXIDIZER_BOOST)}（蒸汽引擎可不填）")
        self.engine, self.oxidizer = engine, oxidizer
        self.engine_count, self.use_booster = engine_count, use_booster
        self.cabins = {"指挥舱": 指挥舱, "研究舱": 研究舱, "货舱": 货舱,
                       "燃料舱": 燃料舱, "氧化剂舱": 氧化剂舱}

    @property
    def eff(self):   return ENGINE_EFFICIENCY[self.engine]
    @property
    def boost(self): return OXIDIZER_BOOST.get(self.oxidizer, 0.0)
    @property
    def is_steam(self): return self.engine == "蒸汽引擎"

    def dry_mass(self):
        """不含燃料/氧化剂的火箭自重。"""
        m = ENGINE_MASS[self.engine] * self.engine_count
        m += SOLID_BOOSTER_MASS if self.use_booster else 0.0
        for name, qty in self.cabins.items():
            m += CABIN_UNIT_MASS[name] * qty
        return m


def calc_forward(cfg, fuel, oxidizer):
    """正向：给定燃料/氧化剂 -> 推进、总质量、惩罚、最终到达。"""
    eff = cfg.eff
    thrust = eff * fuel if cfg.is_steam else eff * min(fuel, oxidizer) * cfg.boost
    total = cfg.dry_mass() + fuel + (0.0 if cfg.is_steam else oxidizer)
    penalty = total if total < 4000 else (total / 300.0) ** 3.2
    reach = thrust - penalty
    return {"thrust": thrust, "total_mass": total, "penalty": penalty,
            "reach": reach, "reachable": reach >= 0}


def solve_fuel(cfg, target_reach, ratio=1.0):
    """反向：给定目标到达距离 + 配置 -> 反推最少燃料/氧化剂。
    ratio = 氧化剂/燃料 质量配比（默认1.0等量，避免 min(燃料,氧化剂) 浪费）。"""
    def reach_at(f):
        o = 0.0 if cfg.is_steam else f * ratio
        return calc_forward(cfg, f, o)["reach"]

    if reach_at(0.0) >= target_reach:
        return _pack(cfg, 0.0, target_reach)

    lo, lo_r = 0.0, reach_at(0.0)
    f, f_r, max_f = 1.0, reach_at(1.0), 1e7
    hi = None
    while f <= max_f:
        if f_r >= target_reach:
            hi = f
            break
        if f_r < lo_r:                      # 越过峰顶仍未达标 -> 目标超过最远可达距离
            best_f, best_r = 0.0, reach_at(0.0)
            t, step = 0.0, max(1.0, (f - lo) / 50)
            while t <= max_f:
                r = reach_at(t)
                if r > best_r:
                    best_f, best_r = t, r
                elif r < best_r - 1 and t > best_f + step:
                    break
                t += step
            return {"reachable": False, "target_reach": target_reach,
                    "max_reach": best_r, "max_reach_fuel": best_f,
                    "message": (f"最远约可达 {best_r:.1f} km（约需燃料 {best_f:.0f} kg），"
                                f"达不到目标 {target_reach} km；请减重（减货舱/舱室）"
                                f"或换更高效引擎（液氢）/液氧氧化剂。")}
        lo, lo_r, f, f_r = f, f_r, f * 2, reach_at(f * 2)

    if hi is None:
        return {"reachable": False, "target_reach": target_reach,
                "message": f"燃料超过 {max_f:.0f} kg 仍未达标，请优化配置。"}

    for _ in range(200):
        mid = (lo + hi) / 2
        if reach_at(mid) >= target_reach:
            hi = mid
        else:
            lo = mid
    return _pack(cfg, hi, target_reach)


def _pack(cfg, fuel, target_reach):
    oxidizer = 0.0 if cfg.is_steam else fuel
    res = calc_forward(cfg, fuel, oxidizer)
    res.update({"fuel": fuel, "oxidizer": oxidizer, "dry_mass": cfg.dry_mass(),
                "target_reach": target_reach, "reachable": True, "message": "求解成功"})
    return res


def show(tag, ans):
    print(f"[{tag}] {ans['message']}")
    if ans.get("reachable"):
        print(f"    燃料={ans['fuel']:.1f}kg  氧化剂={ans['oxidizer']:.1f}kg  "
              f"自重={ans['dry_mass']:.0f}  总质量={ans['total_mass']:.0f}  "
              f"惩罚={ans['penalty']:.0f}  推进={ans['thrust']:.0f}  到达={ans['reach']:.0f}km")
    elif "max_reach" in ans:
        print(f"    最远可达={ans['max_reach']:.0f}km（约需燃料{ans['max_reach_fuel']:.0f}kg）")


class RocketCalculatorApp:
    """火箭燃料反推计算器图形界面。"""

    def __init__(self, root):
        self.root = root
        self.root.title("缺氧火箭燃料推算")
        self.root.geometry("960x760")
        self.root.minsize(840, 750)
        self.root.configure(bg="#f3f6fa")
        asset_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
        self._window_icon = tk.PhotoImage(file=str(asset_dir / "rocket-calculator.png"))
        root.iconphoto(True, self._window_icon)
        if sys.platform == "win32":
            root.iconbitmap(str(asset_dir / "rocket-calculator.ico"))

        self._profile_path = self._get_profile_path()
        self.profiles, self.next_rocket_id, self.active_rocket = self._load_profiles()
        self._loading_profile = False
        self._save_callback = None

        profile = self.profiles[self.active_rocket]
        self.engine_var = tk.StringVar(value=profile["engine"])
        self.oxidizer_var = tk.StringVar(value=profile["oxidizer"])
        self.target_var = tk.StringVar(value=profile["target"])
        self.engine_count_var = tk.StringVar(value=profile["engine_count"])
        self.booster_var = tk.BooleanVar(value=profile["use_booster"])
        self.cabin_vars = {
            name: tk.StringVar(value=profile["cabins"][name])
            for name in CABIN_UNIT_MASS
        }

        self._configure_style()
        self._build_layout()
        self._update_engine_fields()
        for variable in (
            self.engine_var, self.oxidizer_var, self.target_var,
            self.engine_count_var, self.booster_var, *self.cabin_vars.values(),
        ):
            variable.trace_add("write", self._on_profile_changed)
        self.root.bind("<Return>", self._calculate)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_save()

    @staticmethod
    def _get_profile_path():
        if sys.platform == "win32":
            data_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        else:
            data_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return data_dir / "OutOFOxygenRocker" / "rockets.json"

    @staticmethod
    def _default_profile():
        return {
            "engine": "石油引擎",
            "oxidizer": "氧石",
            "target": "23784",
            "engine_count": "1",
            "use_booster": False,
            "cabins": {
                "指挥舱": "1", "研究舱": "0", "货舱": "0",
                "燃料舱": "1", "氧化剂舱": "1",
            },
        }

    def _load_profiles(self):
        if not self._profile_path.exists():
            return {"Rocket1": self._default_profile()}, 2, "Rocket1"

        try:
            with self._profile_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            profiles = data["profiles"]
            active = data["active_rocket"]
            next_id = data["next_rocket_id"]
            if (
                not isinstance(profiles, dict)
                or not profiles
                or not all(isinstance(name, str) and isinstance(value, dict)
                           for name, value in profiles.items())
                or not isinstance(active, str)
                or active not in profiles
                or not isinstance(next_id, int)
                or isinstance(next_id, bool)
            ):
                raise ValueError("配置文件格式不正确。")
            for profile in profiles.values():
                cabins = profile.get("cabins")
                if (
                    profile.get("engine") not in ENGINE_EFFICIENCY
                    or profile.get("oxidizer") not in OXIDIZER_BOOST
                    or not isinstance(profile.get("target"), str)
                    or not isinstance(profile.get("engine_count"), str)
                    or not isinstance(profile.get("use_booster"), bool)
                    or not isinstance(cabins, dict)
                    or set(cabins) != set(CABIN_UNIT_MASS)
                    or not all(isinstance(value, str) for value in cabins.values())
                ):
                    raise ValueError("火箭配置内容不完整或无效。")
            return profiles, max(next_id, 1), active
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            messagebox.showerror(
                "配置读取失败",
                f"无法读取火箭配置文件：\n{self._profile_path}\n\n{exc}\n\n"
                "将使用默认配置继续运行。",
                parent=self.root,
            )
            return {"Rocket1": self._default_profile()}, 2, "Rocket1"

    def _profile_snapshot(self):
        return {
            "engine": self.engine_var.get(),
            "oxidizer": self.oxidizer_var.get(),
            "target": self.target_var.get(),
            "engine_count": self.engine_count_var.get(),
            "use_booster": self.booster_var.get(),
            "cabins": {name: variable.get() for name, variable in self.cabin_vars.items()},
        }

    def _on_profile_changed(self, *_args):
        if not self._loading_profile:
            self._schedule_save()

    def _schedule_save(self):
        if self._save_callback is not None:
            self.root.after_cancel(self._save_callback)
        self._save_callback = self.root.after(400, self._save_profiles)

    def _save_profiles(self):
        self._save_callback = None
        self.profiles[self.active_rocket] = self._profile_snapshot()
        data = {
            "profiles": self.profiles,
            "active_rocket": self.active_rocket,
            "next_rocket_id": self.next_rocket_id,
        }
        try:
            self._profile_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._profile_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp_path.replace(self._profile_path)
        except OSError as exc:
            messagebox.showerror(
                "配置保存失败",
                f"无法保存火箭配置：\n{self._profile_path}\n\n{exc}",
                parent=self.root,
            )

    def _configure_style(self):
        style = ttk.Style(self.root)
        style.configure("TFrame", background="#f3f6fa")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f3f6fa", foreground="#253247",
                        font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background="#ffffff", foreground="#253247",
                        font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background="#f3f6fa", foreground="#17263c",
                        font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background="#f3f6fa", foreground="#65748b",
                        font=("Microsoft YaHei UI", 10))
        style.configure("Section.TLabel", background="#ffffff", foreground="#17263c",
                        font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("MetricValue.TLabel", background="#ffffff", foreground="#1265a8",
                        font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("MetricName.TLabel", background="#ffffff", foreground="#65748b",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                        padding=(12, 8))
        style.configure("TEntry", padding=5)
        style.configure("TCombobox", padding=5)

    def _build_layout(self):
        outer = ttk.Frame(self.root, padding=(26, 22))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        ttk.Label(header, text="缺氧火箭燃料推算", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="输入目标里程和火箭配置，按 Enter 或点击按钮计算所需的最少燃料与氧化剂。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        config = ttk.Frame(outer, style="Card.TFrame", padding=20)
        config.grid(row=1, column=0, sticky="nsew", padx=(0, 9))
        config.columnconfigure(1, weight=1)
        result = ttk.Frame(outer, style="Card.TFrame", padding=20)
        result.grid(row=1, column=1, sticky="nsew", padx=(9, 0))
        result.columnconfigure(0, weight=1)

        self._build_config(config)
        self._build_result(result)

        ttk.Label(
            outer,
            text="计算结果为脚本模型估算；实际游戏中请结合火箭舱室和燃料装载情况确认。",
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(14, 0))

    def _build_config(self, parent):
        ttk.Label(parent, text="火箭配置", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 16)
        )
        profile_controls = ttk.Frame(parent, style="Card.TFrame")
        profile_controls.grid(row=0, column=1, sticky="ew", pady=(0, 16))
        profile_controls.columnconfigure(0, weight=1)
        self.profile_combo = ttk.Combobox(
            profile_controls, values=list(self.profiles), state="readonly", width=10,
        )
        self.profile_combo.set(self.active_rocket)
        self.profile_combo.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.profile_combo.bind("<<ComboboxSelected>>", self._switch_profile)
        ttk.Button(profile_controls, text="新增", command=self._add_profile).grid(
            row=0, column=1, padx=4
        )
        ttk.Button(profile_controls, text="重命名", command=self._rename_profile).grid(
            row=0, column=2, padx=(4, 0)
        )
        ttk.Button(profile_controls, text="删除", command=self._delete_profile).grid(
            row=0, column=3, padx=(4, 0)
        )

        ttk.Label(parent, text="目标里程（km）", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=6
        )
        ttk.Entry(parent, textvariable=self.target_var).grid(
            row=1, column=1, sticky="ew", pady=6
        )

        ttk.Label(parent, text="引擎类型", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=6
        )
        self.engine_combo = ttk.Combobox(
            parent, textvariable=self.engine_var,
            values=list(ENGINE_EFFICIENCY), state="readonly",
        )
        self.engine_combo.grid(row=2, column=1, sticky="ew", pady=6)
        self.engine_combo.bind("<<ComboboxSelected>>", self._update_engine_fields)

        ttk.Label(parent, text="氧化剂", style="Card.TLabel").grid(
            row=3, column=0, sticky="w", pady=6
        )
        self.oxidizer_combo = ttk.Combobox(
            parent, textvariable=self.oxidizer_var,
            values=list(OXIDIZER_BOOST), state="readonly",
        )
        self.oxidizer_combo.grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Label(parent, text="引擎数量", style="Card.TLabel").grid(
            row=4, column=0, sticky="w", pady=6
        )
        ttk.Entry(parent, textvariable=self.engine_count_var).grid(
            row=4, column=1, sticky="ew", pady=6
        )

        self.booster_check = ttk.Checkbutton(
            parent, text="安装固燃推进器（增加 1000 kg 自重）",
            variable=self.booster_var,
        )
        self.booster_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 12))

        ttk.Separator(parent).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(parent, text="舱室数量", style="Section.TLabel").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        self.cabin_entries = {}
        for row, name in enumerate(self.cabin_vars, start=8):
            ttk.Label(parent, text=name, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=4
            )
            entry = ttk.Entry(parent, textvariable=self.cabin_vars[name], width=10)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            self.cabin_entries[name] = entry

        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(
            actions, text="开始推算", style="Primary.TButton", command=self._calculate,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(actions, text="恢复默认", command=self._reset).grid(
            row=0, column=1, sticky="ew", padx=(6, 0)
        )

    def _switch_profile(self, _event=None):
        selected = self.profile_combo.get()
        if selected == self.active_rocket:
            return
        self.profiles[self.active_rocket] = self._profile_snapshot()
        self.active_rocket = selected
        self._load_profile_into_fields(selected)
        self._schedule_save()

    def _add_profile(self):
        while f"Rocket{self.next_rocket_id}" in self.profiles:
            self.next_rocket_id += 1
        name = f"Rocket{self.next_rocket_id}"
        self.next_rocket_id += 1
        self.profiles[self.active_rocket] = self._profile_snapshot()
        self.profiles[name] = self._default_profile()
        self.active_rocket = name
        self.profile_combo.configure(values=list(self.profiles))
        self.profile_combo.set(name)
        self._load_profile_into_fields(name)
        self._schedule_save()

    def _rename_profile(self):
        name = simpledialog.askstring(
            "重命名火箭", "请输入新的火箭名称：",
            initialvalue=self.active_rocket, parent=self.root,
        )
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("名称无效", "火箭名称不能为空。", parent=self.root)
            return
        if name != self.active_rocket and name in self.profiles:
            messagebox.showerror("名称已存在", "已有同名火箭，请使用其他名称。", parent=self.root)
            return
        self.profiles[self.active_rocket] = self._profile_snapshot()
        profile = self.profiles.pop(self.active_rocket)
        self.profiles[name] = profile
        self.active_rocket = name
        self.profile_combo.configure(values=list(self.profiles))
        self.profile_combo.set(name)
        self._schedule_save()

    def _delete_profile(self):
        if len(self.profiles) == 1:
            messagebox.showwarning(
                "无法删除", "至少需要保留一个火箭配置。", parent=self.root
            )
            return
        if not messagebox.askyesno(
            "删除火箭",
            f"确定删除火箭“{self.active_rocket}”吗？此操作无法撤销。",
            parent=self.root,
        ):
            return

        self.profiles[self.active_rocket] = self._profile_snapshot()
        del self.profiles[self.active_rocket]
        self.active_rocket = next(iter(self.profiles))
        self.profile_combo.configure(values=list(self.profiles))
        self.profile_combo.set(self.active_rocket)
        self._load_profile_into_fields(self.active_rocket)
        self._schedule_save()

    def _load_profile_into_fields(self, name):
        profile = self.profiles[name]
        self._loading_profile = True
        try:
            self.engine_var.set(profile["engine"])
            self.oxidizer_var.set(profile["oxidizer"])
            self.target_var.set(profile["target"])
            self.engine_count_var.set(profile["engine_count"])
            self.booster_var.set(profile["use_booster"])
            for cabin_name, variable in self.cabin_vars.items():
                variable.set(profile["cabins"][cabin_name])
        finally:
            self._loading_profile = False
        self._update_engine_fields()
        self._reset_result()

    def _reset_result(self):
        self.result_status.configure(text="填写配置后点击“开始推算”。", foreground="#253247")
        self.result_detail.configure(
            text="使用脚本中的质量、推进效率与载重惩罚公式进行反推。"
        )
        for value in self.metric_values.values():
            value.configure(text="—")

    def _on_close(self):
        if self._save_callback is not None:
            self.root.after_cancel(self._save_callback)
        self._save_profiles()
        self.root.destroy()

    def _build_result(self, parent):
        ttk.Label(parent, text="推算结果", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 12)
        )
        self.result_status = ttk.Label(
            parent, text="填写配置后点击“开始推算”。", style="Card.TLabel",
            wraplength=330, justify="left",
        )
        self.result_status.grid(row=1, column=0, sticky="w", pady=(0, 14))

        metrics = ttk.Frame(parent, style="Card.TFrame")
        metrics.grid(row=2, column=0, sticky="ew")
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)
        self.metric_values = {}
        for index, (key, label) in enumerate((
            ("fuel", "所需燃料"),
            ("oxidizer", "所需氧化剂"),
            ("dry_mass", "火箭自重"),
            ("total_mass", "总质量"),
            ("thrust", "推进距离"),
            ("penalty", "载重惩罚"),
        )):
            row, col = divmod(index, 2)
            cell = ttk.Frame(metrics, style="Card.TFrame", padding=(0, 8, 8, 8))
            cell.grid(row=row, column=col, sticky="ew")
            value = ttk.Label(cell, text="—", style="MetricValue.TLabel")
            value.pack(anchor="w")
            ttk.Label(cell, text=label, style="MetricName.TLabel").pack(anchor="w")
            self.metric_values[key] = value

        ttk.Separator(parent).grid(row=3, column=0, sticky="ew", pady=14)
        ttk.Label(parent, text="结果说明", style="Section.TLabel").grid(
            row=4, column=0, sticky="w", pady=(0, 8)
        )
        self.result_detail = ttk.Label(
            parent, text="使用脚本中的质量、推进效率与载重惩罚公式进行反推。",
            style="Card.TLabel", wraplength=330, justify="left",
        )
        self.result_detail.grid(row=5, column=0, sticky="nw")

    def _update_engine_fields(self, _event=None):
        is_steam = self.engine_var.get() == "蒸汽引擎"
        self.oxidizer_combo.configure(state="disabled" if is_steam else "readonly")
        for name in ("燃料舱", "氧化剂舱"):
            self.cabin_entries[name].configure(state="disabled" if is_steam else "normal")

    @staticmethod
    def _read_count(value, label):
        try:
            count = int(value)
        except ValueError as exc:
            raise ValueError(f"{label}请输入非负整数。") from exc
        if count < 0:
            raise ValueError(f"{label}不能小于 0。")
        return count

    def _calculate(self, _event=None):
        try:
            try:
                target = float(self.target_var.get())
            except ValueError as exc:
                raise ValueError("目标里程请输入有效数字。") from exc
            if not math.isfinite(target) or target < 0:
                raise ValueError("目标里程请输入大于或等于 0 的有效数字。")
            engine_count = self._read_count(self.engine_count_var.get(), "引擎数量")
            cabins = {
                name: self._read_count(var.get(), name)
                for name, var in self.cabin_vars.items()
            }
        except ValueError as exc:
            self.result_status.configure(text="输入有误", foreground="#b5473c")
            self.result_detail.configure(text=str(exc))
            for value in self.metric_values.values():
                value.configure(text="—")
            messagebox.showerror("输入有误", str(exc), parent=self.root)
            return

        is_steam = self.engine_var.get() == "蒸汽引擎"
        if is_steam:
            cabins["燃料舱"] = 0
            cabins["氧化剂舱"] = 0
        cfg = RocketConfig(
            self.engine_var.get(),
            self.oxidizer_var.get(),
            engine_count,
            self.booster_var.get(),
            **cabins,
        )
        answer = solve_fuel(cfg, target)
        self._show_result(answer)

    def _show_result(self, answer):
        if not answer["reachable"]:
            self.result_status.configure(text="未找到可行方案", foreground="#b5473c")
            for value in self.metric_values.values():
                value.configure(text="—")
            detail = answer["message"]
            if "max_reach" in answer:
                detail += (
                    f"\n\n最远可达：{answer['max_reach']:,.1f} km"
                    f"\n对应燃料：{answer['max_reach_fuel']:,.1f} kg"
                )
            self.result_detail.configure(text=detail)
            return

        self.result_status.configure(text="推算成功：以下为满足目标的最少燃料配置。",
                                     foreground="#27744a")
        self.metric_values["fuel"].configure(text=f"{answer['fuel']:,.1f} kg")
        self.metric_values["oxidizer"].configure(text=f"{answer['oxidizer']:,.1f} kg")
        self.metric_values["dry_mass"].configure(text=f"{answer['dry_mass']:,.0f} kg")
        self.metric_values["total_mass"].configure(text=f"{answer['total_mass']:,.0f} kg")
        self.metric_values["thrust"].configure(text=f"{answer['thrust']:,.1f} km")
        self.metric_values["penalty"].configure(text=f"{answer['penalty']:,.1f} km")
        self.result_detail.configure(
            text=f"目标里程：{answer['target_reach']:,.1f} km\n"
                 f"预计到达：{answer['reach']:,.1f} km\n"
                 f"{answer['message']}"
        )

    def _reset(self):
        profile = self._default_profile()
        self.engine_var.set(profile["engine"])
        self.oxidizer_var.set(profile["oxidizer"])
        self.target_var.set(profile["target"])
        self.engine_count_var.set(profile["engine_count"])
        self.booster_var.set(profile["use_booster"])
        for name, value in profile["cabins"].items():
            self.cabin_vars[name].set(value)
        self._update_engine_fields()
        self._reset_result()


if __name__ == "__main__":
    root = tk.Tk()
    RocketCalculatorApp(root)
    root.mainloop()
