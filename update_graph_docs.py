import os
import sys
import importlib.util
from datetime import datetime

# 스캔할 그래프 폴더와 결과물을 저장할 위키 경로
GRAPHS_DIR = os.path.join(os.getcwd(), "graphs")
WIKI_OUTPUT_PATH = (
    "/home/ubuntu/llm-wiki/03_Resources/General_Resources/System_Graph_Architectures.md"
)


def get_graph_objects(module):
    """모듈 내에서 LangGraph 객체(get_graph 메서드가 있는 객체)를 찾습니다."""
    graphs = {}
    for name, obj in vars(module).items():
        if hasattr(obj, "get_graph"):
            try:
                graph_instance = obj.get_graph()
                if hasattr(graph_instance, "draw_mermaid"):
                    graphs[name] = obj
            except TypeError:
                pass
            except Exception:
                pass
    return graphs


def generate_docs():
    print(f"🔍 '{GRAPHS_DIR}' 폴더의 그래프들을 스캔합니다...")

    markdown_lines = [
        "# 🧠 Sonagi System Graph Architectures",
        f"\n*마지막 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n",
        "> 💡 이 문서는 시스템에 정의된 LangGraph 아키텍처 코드를 스캔하여 자동으로 생성된 최신 다이어그램입니다.\n\n",
    ]

    found_graphs_count = 0

    for filename in os.listdir(GRAPHS_DIR):
        if filename.endswith(".py") and not filename.startswith("__"):
            filepath = os.path.join(GRAPHS_DIR, filename)
            module_name = filename[:-3]

            try:
                spec = importlib.util.spec_from_file_location(module_name, filepath)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                graphs = get_graph_objects(module)

                for graph_name, graph_obj in graphs.items():
                    found_graphs_count += 1
                    print(f"   -> 🧩 다이어그램 추출 성공: {filename} ({graph_name})")

                    mermaid_syntax = graph_obj.get_graph().draw_mermaid()

                    markdown_lines.append(f"## 🛠️ {graph_name}")
                    markdown_lines.append(f"- **Source File**: `graphs/{filename}`\n")
                    markdown_lines.append("```mermaid")
                    markdown_lines.append(mermaid_syntax)
                    markdown_lines.append("```\n")
                    markdown_lines.append("---\n")

            except Exception as e:
                print(f"   -> ⚠️ 스킵됨: {filename} (사유: {str(e)})")

    if found_graphs_count > 0:
        os.makedirs(os.path.dirname(WIKI_OUTPUT_PATH), exist_ok=True)
        with open(WIKI_OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(markdown_lines))
        print(
            f"\n✅ 성공! 총 {found_graphs_count}개의 다이어그램이 위키에 업데이트 되었습니다."
        )
        print(f"📂 저장 경로: {WIKI_OUTPUT_PATH}")
    else:
        print("\n❌ 추출할 수 있는 그래프 객체를 찾지 못했습니다.")


if __name__ == "__main__":
    generate_docs()
