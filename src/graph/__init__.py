"""AgentCore Platform v1.0"""

# Registry entry point — re-export the agent class from the package.
#
# config/agent.yaml declares `class: "src.graph.graph.CustomsDocumentationGeneratorAgent"`,
# and the registry lazy-loads the agent by importing this package and reading
# that attribute from it. Re-exporting the class (and its `Graph` alias) here is
# what makes manifest-based discovery resolve.
from src.graph.graph import CustomsDocumentationGeneratorAgent, Graph

__all__ = ["CustomsDocumentationGeneratorAgent", "Graph"]
