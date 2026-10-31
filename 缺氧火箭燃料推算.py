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

import math
import sys
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

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
        self.root.geometry("860x700")
        self.root.minsize(760, 640)
        self.root.configure(bg="#f3f6fa")
        asset_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
        self._window_icon = tk.PhotoImage(file=str(asset_dir / "rocket-calculator.png"))
        root.iconphoto(True, self._window_icon)
        if sys.platform == "win32":
            root.iconbitmap(str(asset_dir / "rocket-calculator.ico"))

        self.engine_var = tk.StringVar(value="石油引擎")
        self.oxidizer_var = tk.StringVar(value="氧石")
        self.target_var = tk.StringVar(value="23784")
        self.engine_count_var = tk.StringVar(value="1")
        self.booster_var = tk.BooleanVar(value=False)
        self.cabin_vars = {
            "指挥舱": tk.StringVar(value="1"),
            "研究舱": tk.StringVar(value="0"),
            "货舱": tk.StringVar(value="0"),
            "燃料舱": tk.StringVar(value="1"),
            "氧化剂舱": tk.StringVar(value="1"),
        }

        self._configure_style()
        self._build_layout()
        self._update_engine_fields()

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
            text="输入目标里程和火箭配置，计算达到目标所需的最少燃料与氧化剂。",
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
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 16)
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

    def _calculate(self):
        try:
            target = float(self.target_var.get())
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
        self.engine_var.set("石油引擎")
        self.oxidizer_var.set("氧石")
        self.target_var.set("23784")
        self.engine_count_var.set("1")
        self.booster_var.set(False)
        defaults = {"指挥舱": "1", "研究舱": "0", "货舱": "0",
                    "燃料舱": "1", "氧化剂舱": "1"}
        for name, value in defaults.items():
            self.cabin_vars[name].set(value)
        self._update_engine_fields()
        self.result_status.configure(text="填写配置后点击“开始推算”。", foreground="#253247")
        self.result_detail.configure(
            text="使用脚本中的质量、推进效率与载重惩罚公式进行反推。"
        )
        for value in self.metric_values.values():
            value.configure(text="—")


if __name__ == "__main__":
    root = tk.Tk()
    RocketCalculatorApp(root)
    root.mainloop()
