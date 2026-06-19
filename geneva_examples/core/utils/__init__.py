"""Generic, Geneva-agnostic helpers for the package.

Import the concrete helpers from their submodules (``geneva_examples.core.utils.retry``,
``geneva_examples.core.utils.images``, ``geneva_examples.core.utils.tables``) so importing this
package does not eagerly pull in heavy dependencies like PyArrow.
"""
