"""Graph diagram rendering, shared by every builder."""

from config import GRAPH_IMAGE_DIR


def save_graph_image(graph, name: str) -> None:
    """Render a compiled graph to GRAPH_IMAGE_DIR/<name>.png.

    Diagram rendering is a convenience, never a reason to fail a run, so any
    failure here is reported and swallowed.
    """
    try:
        GRAPH_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        png_data = graph.get_graph().draw_mermaid_png()
        (GRAPH_IMAGE_DIR / f"{name}.png").write_bytes(png_data)
    except Exception as e:
        print(f"Could not render {name}.png ({type(e).__name__}): {e}")
