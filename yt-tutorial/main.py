from super_builder import super_graph

for s in super_graph.stream(
    {
        "messages": [
            {"role": "user", "content": "Research the open source tools available for building a document writing assistant. Come up with at least 3 tools, provide a brief description of each, and compile in a file named 'research_results.txt'."}
        ]
    },
    {
        "recursion_limit": 50,
    }
):
    print(s)
    print("----------------------------------------------------------")
    