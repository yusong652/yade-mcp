# Re-equilibrate after erasing escapees: re-run iso compaction at 100 kPa
# until stress and unbalanced force settle, then re-save.

triax.updatePorosity = True  # tell controller to recompute solid volume next step
target_kPa = 100.0
stress_tol_kPa = 0.5
unb_tol = 0.01


def checkReequilib():
    s_kPa = -triax.meanStress / 1e3
    unb = unbalancedForce()
    n = triax.porosity
    e = n / (1 - n) if n < 1.0 else float("inf")
    print(
        f"iter={O.iter:>6d}  s_mean={s_kPa:7.2f} kPa  "
        f"unb={unb:.3e}  n={n:.4f}  e={e:.4f}"
    )
    if abs(s_kPa - target_kPa) < stress_tol_kPa and unb < unb_tol:
        print(
            f"=== RE-EQUILIBRATED at iter={O.iter}: s_mean={s_kPa:.2f} kPa, "
            f"unb={unb:.3e}, e0={e:.4f}, n0={n:.4f} ==="
        )
        O.pause()


O.engines = [e for e in O.engines if not (hasattr(e, "label") and e.label == "checker")]
O.engines = O.engines + [
    PyRunner(command="checkReequilib()", iterPeriod=200, label="checker")
]

n_spheres = sum(1 for b in O.bodies if isinstance(b.shape, Sphere))
print(f"[reequilib] {n_spheres} spheres after cleanup; running ...")
O.run(50000, wait=True)

s_final = -triax.meanStress / 1e3
n_final = triax.porosity
e_final = n_final / (1 - n_final)
print(
    f"[done] iter={O.iter}  s_mean={s_final:.2f} kPa  n={n_final:.4f}  e={e_final:.4f}"
)

save_path = "triax_consolidated_100kPa.yade.bz2"
O.save(save_path)
print(f"[save] state overwritten at {save_path}")
