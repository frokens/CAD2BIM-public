"""Domain contracts are imported from their owning modules.

Keep package initialization dependency-free so isolated geometry and data
workers do not import unrelated control-plane contracts.
"""
