# Drained triaxial — Stage 2: drained shear to ε_axial = 15 %
#
# Loads the consolidated state, restores friction to 30°, switches the triax
# controller to strain-rate control on the y-axis and stress control on x/z
# at 100 kPa, then shears until ε_axial = 0.15 and saves a stress-strain plot.
#
# Sign conventions: triax uses continuum mechanics (positive = tension), so
# compressive targets are negative. After O.load the engine's strain
# accumulator reads zero, so we use triax.strain directly (no baseline).
from math import radians, tan
from yade import plot

# ---------- load consolidated state ----------
O.load("/workspace/triax_consolidated_100kPa.yade.bz2")
triax = [e for e in O.engines if getattr(e, "label", "") == "triax"][0]
print(f"[load] iter={O.iter} σ_mean={-triax.meanStress/1e3:.2f} kPa  "
      f"box=({triax.width:.4f}, {triax.height:.4f}, {triax.depth:.4f}) m")

# ---------- restore friction ----------
finalFricRad = radians(30.0)
mat = O.materials["spheres"]
mat.frictionAngle = finalFricRad
tan_phi = tan(finalFricRad)
n_updated = 0
for i in O.interactions:
    if i.isReal:
        i.phys.tangensOfFrictionAngle = tan_phi
        n_updated += 1
print(f"[shear] friction restored to 30° on {n_updated} active contacts")

# ---------- switch triax to shear mode ----------
target_lat_kPa = 100.0
strain_rate = 0.1            # 1/s (compression in y)
target_axial_strain = 0.15

triax.stressMask = 5         # 0b101: x and z stress, y strain rate
triax.goal1 = -target_lat_kPa * 1e3
triax.goal2 = -strain_rate
triax.goal3 = -target_lat_kPa * 1e3
triax.internalCompaction = False
triax.max_vel = 10.0

# Capture a strain baseline AFTER the engine has populated its strain readout.
# triax updates strain every computeStressStrainInterval=10 iters, and the
# engine's internal reference comes from the original 50 mm box during
# consolidation — so triax.strain after O.load reads ≈ -0.97 in the y-axis
# once it ticks. We tick a few cycles to settle and snapshot, then subtract
# the baseline below so εa, εv read from zero at shear start.
O.run(triax.computeStressStrainInterval * 3, wait=True)
strain0 = Vector3(triax.strain)
print(f"[shear] strain baseline after warm-up ticks: {strain0}")


# ---------- recorder ----------
def recordData():
    sx = -0.5 * (triax.stress(0)[0] + triax.stress(1)[0])
    sy = -0.5 * (triax.stress(2)[1] + triax.stress(3)[1])
    sz = -0.5 * (triax.stress(4)[2] + triax.stress(5)[2])
    s_lat = 0.5 * (sx + sz)
    ex = triax.strain[0] - strain0[0]
    ey = triax.strain[1] - strain0[1]
    ez = triax.strain[2] - strain0[2]
    e_axial = -ey
    e_vol = -(ex + ey + ez)
    q = sy - s_lat
    p_mean = (sy + 2.0 * s_lat) / 3.0
    n = triax.porosity
    void = n / (1.0 - n) if n < 1.0 else float("inf")
    unb = unbalancedForce()

    plot.addData(
        iter=O.iter,
        ea=e_axial, ev=e_vol,
        q=q / 1e3, p=p_mean / 1e3,
        sa=sy / 1e3, sl=s_lat / 1e3,
        e=void, unb=unb,
    )

    if O.iter % 2000 == 0 or e_axial >= target_axial_strain:
        print(
            f"iter={O.iter:>7d}  ea={e_axial*100:6.2f}%  ev={e_vol*100:+6.3f}%  "
            f"q={q/1e3:7.2f} kPa  p={p_mean/1e3:7.2f} kPa  "
            f"σa={sy/1e3:7.2f}  σl={s_lat/1e3:7.2f}  e={void:.4f}  unb={unb:.2e}"
        )
    if e_axial >= target_axial_strain:
        print(f"=== TARGET ε_axial = {target_axial_strain*100:.1f} % reached ===")
        O.pause()


O.engines = [
    e for e in O.engines
    if not (hasattr(e, "label") and e.label in ("checker", "recorder"))
]
O.engines = O.engines + [
    PyRunner(command="recordData()", iterPeriod=200, label="recorder")
]

plot.reset()

print(
    f"[shear] σ_lat=100 kPa, ε̇_axial=-{strain_rate}/s, target ε_axial="
    f"{target_axial_strain*100:.0f}%, dt={O.dt:.2e} s, "
    f"expected total time ≈ {target_axial_strain/strain_rate:.2f} s"
)

O.run(2_000_000, wait=True)

# ---------- final state ----------
ex_f = triax.strain[0] - strain0[0]
ey_f = triax.strain[1] - strain0[1]
ez_f = triax.strain[2] - strain0[2]
ea_f = -ey_f
ev_f = -(ex_f + ey_f + ez_f)
sx_f = -0.5 * (triax.stress(0)[0] + triax.stress(1)[0])
sy_f = -0.5 * (triax.stress(2)[1] + triax.stress(3)[1])
sz_f = -0.5 * (triax.stress(4)[2] + triax.stress(5)[2])
sl_f = 0.5 * (sx_f + sz_f)
q_f = sy_f - sl_f
n_f = triax.porosity
e_f = n_f / (1 - n_f)
print(
    f"[done] iter={O.iter}  ea={ea_f*100:.2f}%  ev={ev_f*100:+.3f}%  "
    f"q={q_f/1e3:.2f} kPa  σa={sy_f/1e3:.2f}  σl={sl_f/1e3:.2f}  e={e_f:.4f}"
)

# ---------- save state + raw curves ----------
import pickle
state_path = "/workspace/triax_sheared_eps15.yade.bz2"
data_path = "/workspace/triax_drained_shear_data.pkl"
O.save(state_path)
with open(data_path, "wb") as fh:
    pickle.dump(plot.data, fh)
print(f"[save] state → {state_path}")
print(f"[save] curves → {data_path}")

# ---------- stress-strain plots (matplotlib, headless) ----------
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ea_pct = [v * 100 for v in plot.data["ea"]]
ev_pct = [v * 100 for v in plot.data["ev"]]
qq = list(plot.data["q"])
pp = list(plot.data["p"])
ee = list(plot.data["e"])

fig, axes = plt.subplots(2, 2, figsize=(11, 8))
ax = axes[0, 0]
ax.plot(ea_pct, qq, "C0-")
ax.set_xlabel(r"axial strain $\varepsilon_a$  [%]")
ax.set_ylabel(r"deviator stress  $q = \sigma_a - \sigma_l$  [kPa]")
ax.set_title("Stress–strain")
ax.grid(True, alpha=0.4)

ax = axes[0, 1]
ax.plot(ea_pct, ev_pct, "C1-")
ax.axhline(0, color="0.6", lw=0.7)
ax.set_xlabel(r"axial strain $\varepsilon_a$  [%]")
ax.set_ylabel(r"volumetric strain $\varepsilon_v$  [%]   (+ compaction)")
ax.set_title("Volume change")
ax.grid(True, alpha=0.4)

ax = axes[1, 0]
ax.plot(pp, qq, "C2-")
ax.set_xlabel(r"mean stress  $p$  [kPa]")
ax.set_ylabel(r"deviator stress  $q$  [kPa]")
ax.set_title("Stress path  (drained: $\\Delta p = \\Delta q / 3$)")
ax.grid(True, alpha=0.4)

ax = axes[1, 1]
ax.plot(ea_pct, ee, "C3-")
ax.set_xlabel(r"axial strain $\varepsilon_a$  [%]")
ax.set_ylabel(r"void ratio $e$")
ax.set_title("Void ratio")
ax.grid(True, alpha=0.4)

fig.suptitle(
    f"Drained triaxial — 100 kPa, $\\varphi$=30°, "
    f"{sum(1 for b in O.bodies if isinstance(b.shape, Sphere))} spheres",
    fontsize=12,
)
fig.tight_layout(rect=(0, 0, 1, 0.96))

plot_path = "/workspace/triax_drained_shear_curves.png"
fig.savefig(plot_path, dpi=140)
print(f"[save] plot → {plot_path}")
