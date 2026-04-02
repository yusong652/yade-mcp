"""
yade-mcp header generation script

Design philosophy:
  Three light ribbons tell the story of LLM-DEM connection through MCP.

  - Ribbon_Warm (orange): Damped cosine oscillation
    z = A·e^(-β(x+1.5))·cos(ω(x+1.5)) + base
    Represents DEM particle dynamics — collision, vibration, energy dissipation.
    High amplitude on the left (energetic collisions), decaying toward the right.

  - Ribbon_Cool (blue): Sigmoid activation
    z = L / (1 + e^(-k(x-x₀))) + offset
    Represents LLM intelligence emergence — information activating from dormant
    to fully engaged. Rises smoothly from left to right.

  - Ribbon_Green (green): Horizontal baseline
    z = const
    Represents MCP protocol layer — stable, reliable, always-on infrastructure.
    The quiet foundation that enables the connection above.

  The orange and blue ribbons cross near the center ("yade-mcp" text),
  symbolizing the moment where physics meets intelligence.

Animation:
  - Orange sweeps left→right (DEM collision wave propagates)
  - Blue sweeps left→right (LLM activation spreads)
  - Green fades in (MCP channel established)
  - Slogan appears with green ribbon, lingers after ribbons fade

Slogan overlay (via Pillow, not Blender):
  "O.engines += [LLM()]  # yet another engine."
  Font: JetBrains Mono Regular, 27px
  Fade in: F55-70, hold: F70-105, fade out: F105-118

Requirements:
  - Blender 3.x+ with Cycles
  - JetBrains Mono font (for slogan overlay)
  - Pillow (for GIF assembly + slogan)
"""

import math

# ==============================================================================
# Curve equations
# ==============================================================================

def damped_cosine(x):
    """DEM: damped cosine — particle vibration with energy dissipation"""
    A = 0.36       # initial amplitude
    beta = 0.6     # decay rate
    omega = 6.0    # angular frequency
    base = 0.12    # equilibrium position
    return A * math.exp(-beta * (x + 1.5)) * math.cos(omega * (x + 1.5)) + base


def sigmoid_activation(x):
    """LLM: sigmoid — intelligence emergence / activation function"""
    L = 0.42       # maximum value
    k = 2.0        # steepness
    x0 = 0.826     # midpoint (center of text)
    offset = -0.03  # vertical offset
    return L / (1.0 + math.exp(-k * (x - x0))) + offset


def mcp_baseline(_x):
    """MCP: constant — stable protocol layer"""
    return -0.03


# ==============================================================================
# Blender scene parameters
# ==============================================================================

# Control points sampled along curves (aligned to camera viewport)
CONTROL_X = [-0.37, 0.05, 0.3, 0.6, 0.826, 1.1, 1.4, 1.7, 2.03]

RIBBONS = {
    "Ribbon_Warm":  {"func": damped_cosine,      "y_base": 0.12, "color": (1.0, 0.6, 0.2)},
    "Ribbon_Cool":  {"func": sigmoid_activation,  "y_base": 0.25, "color": (0.15, 0.5, 1.0)},
    "Ribbon_Green": {"func": mcp_baseline,         "y_base": 0.18, "color": (0.1, 0.9, 0.3)},
}

# Camera (orthographic)
CAMERA = {
    "location": (0.826, -2.0, 0.306),
    "ortho_scale": 2.4,
    "visible_x": (-0.374, 2.026),
    "visible_z": (-0.032, 0.644),
}

# Render
RENDER = {
    "resolution": (1440, 405),
    "engine": "CYCLES",
    "device": "GPU",  # OptiX preferred for RTX
    "frames": 120,
    "fps": 30,
}

# Animation keyframes
ANIMATION = {
    "Ribbon_Warm": {
        "sweep_progress": [(1, -0.3), (35, 1.3), (120, 1.3)],
        "master_fade":    [(1, 0), (3, 1), (88, 1), (112, 0), (120, 0)],
    },
    "Ribbon_Cool": {
        "sweep_progress": [(1, -0.3), (15, -0.3), (50, 1.3), (120, 1.3)],
        "master_fade":    [(1, 0), (3, 1), (88, 1), (112, 0), (120, 0)],
    },
    "Ribbon_Green": {
        "green_strength": [(1, 0), (45, 0), (58, 1), (88, 1), (112, 0), (120, 0)],
    },
}

# Slogan overlay (applied via Pillow post-render)
SLOGAN = {
    "text": "O.engines += [LLM()]  # yet another engine.",
    "font": "JetBrainsMono-Regular.ttf",
    "font_size": 27,
    "color": (180, 180, 180),
    "fade_in":  (55, 70),    # frames
    "hold":     (70, 105),
    "fade_out": (105, 118),
}


# ==============================================================================
# GIF assembly with slogan overlay
# ==============================================================================

def get_slogan_alpha(frame):
    """Returns text alpha (0-180) for the slogan at given frame."""
    fi_start, fi_end = SLOGAN["fade_in"]
    fo_start, fo_end = SLOGAN["fade_out"]
    max_alpha = 180

    if frame < fi_start:
        return 0
    elif frame <= fi_end:
        return int(max_alpha * (frame - fi_start) / (fi_end - fi_start))
    elif frame <= fo_start:
        return max_alpha
    elif frame <= fo_end:
        return int(max_alpha * (fo_end - frame) / (fo_end - fo_start))
    else:
        return 0


def assemble_gif(frames_dir, output_path):
    """Assemble rendered frames into a GIF with slogan overlay."""
    from PIL import Image, ImageDraw, ImageFont
    import glob

    font = ImageFont.truetype(SLOGAN["font"], SLOGAN["font_size"])
    frames_paths = sorted(glob.glob(f"{frames_dir}/frame_*.png"))

    sample = Image.open(frames_paths[0])
    w, h = sample.size
    bbox = font.getbbox(SLOGAN["text"])
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (w - text_w) // 2
    text_y = h - text_h - 15

    imgs = []
    for i, path in enumerate(frames_paths):
        frame_num = i + 1
        img = Image.open(path).convert("RGBA")

        alpha = get_slogan_alpha(frame_num)
        if alpha > 0:
            overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            r, g, b = SLOGAN["color"]
            draw.text((text_x, text_y), SLOGAN["text"], font=font, fill=(r, g, b, alpha))
            img = Image.alpha_composite(img, overlay)

        imgs.append(img.convert("RGB"))

    imgs[0].save(
        output_path,
        save_all=True,
        append_images=imgs[1:],
        duration=1000 // RENDER["fps"],
        loop=0,
        optimize=True,
    )
    print(f"GIF saved: {output_path}")


if __name__ == "__main__":
    assemble_gif("/tmp/header_anim_v2", "/tmp/header_final.gif")
