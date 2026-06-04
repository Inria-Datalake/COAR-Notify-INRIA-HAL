import csv
import logging
import os

logger = logging.getLogger(__name__)

# Global blacklist cache
_blacklist_cache: set[str] = set()
_blacklist_file_path = "./app/static/data/blacklist.csv"


class BlacklistManager:
    """
    Centralized blacklist management for filtering software names.

    This class handles loading, caching, and managing the blacklist
    used to filter out generic or non-software terms from software detection.
    """

    def __init__(self, blacklist_path: str = None):
        """
        Initialize the blacklist manager.

        Args:
            blacklist_path: Path to the blacklist CSV file
        """
        self.blacklist_path = blacklist_path or _blacklist_file_path
        self.load_blacklist()

    def load_blacklist(self) -> set[str]:
        """
        Load blacklist terms from CSV file into cache.

        Returns:
            Set of blacklisted terms
        """
        global _blacklist_cache

        try:
            blacklist = set()
            if os.path.exists(self.blacklist_path):
                with open(self.blacklist_path, newline="", encoding="utf-8") as csvfile:
                    reader = csv.reader(csvfile)
                    # Skip header if exists
                    first_row = next(reader, None)
                    if first_row and first_row[0].lower() in ["term", "word", "pattern"]:
                        pass  # Header row, already skipped

                    for row in reader:
                        if row and row[0]:
                            # Clean up the term
                            term = row[0].strip()
                            if term and term != "":
                                blacklist.add(term)

                _blacklist_cache = blacklist
                logger.info(f"Loaded {len(blacklist)} terms from blacklist: {self.blacklist_path}")
            else:
                logger.warning(f"Blacklist file not found: {self.blacklist_path}")
                _blacklist_cache = set()

        except Exception as e:
            logger.error(f"Failed to load blacklist from {self.blacklist_path}: {e}")
            _blacklist_cache = set()

        return _blacklist_cache

    def get_blacklist(self) -> set[str]:
        """
        Get the current blacklist from cache.

        Returns:
            Set of blacklisted terms
        """
        return _blacklist_cache.copy()

    def is_blacklisted(self, term: str) -> bool:
        """
        Check if a term is in the blacklist.

        Args:
            term: Term to check

        Returns:
            True if term is blacklisted, False otherwise
        """
        return term in _blacklist_cache

    def add_to_blacklist(self, term: str) -> bool:
        """
        Add a term to the blacklist.

        Args:
            term: Term to add

        Returns:
            True if added successfully, False if already exists
        """
        if not term or not term.strip():
            return False

        term = term.strip()
        if term in _blacklist_cache:
            return False

        _blacklist_cache.add(term)
        self._save_blacklist()
        logger.info(f"Added term to blacklist: {term}")
        return True

    def remove_from_blacklist(self, term: str) -> bool:
        """
        Remove a term from the blacklist.

        Args:
            term: Term to remove

        Returns:
            True if removed successfully, False if not found
        """
        if term not in _blacklist_cache:
            return False

        _blacklist_cache.remove(term)
        self._save_blacklist()
        logger.info(f"Removed term from blacklist: {term}")
        return True

    def _save_blacklist(self):
        """Save the current blacklist cache to file."""
        try:
            os.makedirs(os.path.dirname(self.blacklist_path), exist_ok=True)
            with open(self.blacklist_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile)
                # Write header
                writer.writerow(["term"])
                # Write all terms
                for term in sorted(_blacklist_cache):
                    writer.writerow([term])
            logger.info(f"Saved {len(_blacklist_cache)} terms to blacklist: {self.blacklist_path}")
        except Exception as e:
            logger.error(f"Failed to save blacklist to {self.blacklist_path}: {e}")

    def reload_blacklist(self) -> int:
        """
        Reload the blacklist from file.

        Returns:
            Number of terms loaded
        """
        blacklist = self.load_blacklist()
        return len(blacklist)

    def get_blacklist_stats(self) -> dict:
        """
        Get statistics about the blacklist.

        Returns:
            Dictionary with blacklist statistics
        """
        return {
            "total_terms": len(_blacklist_cache),
            "file_path": self.blacklist_path,
            "file_exists": os.path.exists(self.blacklist_path),
            "last_loaded": "At startup",  # Could be enhanced with timestamp
        }

    def search_blacklist(self, query: str, limit: int = 50) -> list[str]:
        """
        Search for terms in the blacklist.

        Args:
            query: Search query (case-insensitive)
            limit: Maximum number of results

        Returns:
            List of matching terms
        """
        if not query:
            return []

        query_lower = query.lower()
        matches = [term for term in _blacklist_cache if query_lower in term.lower()]

        return sorted(matches)[:limit]

    def export_blacklist(self) -> str:
        """
        Export blacklist as CSV string.

        Returns:
            CSV string representation of the blacklist
        """
        import io

        output = io.StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow(["term"])

        # Write all terms
        for term in sorted(_blacklist_cache):
            writer.writerow([term])

        return output.getvalue()

    def import_blacklist_from_csv(self, csv_content: str, overwrite: bool = False) -> dict:
        """
        Import blacklist from CSV content.

        Args:
            csv_content: CSV string content
            overwrite: Whether to overwrite existing blacklist

        Returns:
            Dictionary with import results
        """
        try:
            # Parse CSV content
            import io

            csv_file = io.StringIO(csv_content)
            reader = csv.reader(csv_file)

            # Skip header if exists
            first_row = next(reader, None)
            if first_row and first_row[0].lower() in ["term", "word", "pattern"]:
                pass  # Header row

            new_terms = set()
            for row in reader:
                if row and row[0]:
                    term = row[0].strip()
                    if term and term != "":
                        new_terms.add(term)

            if overwrite:
                _blacklist_cache = new_terms
                self._save_blacklist()
                logger.info(f"Overwrote blacklist with {len(new_terms)} terms")
            else:
                # Merge with existing blacklist
                _blacklist_cache.update(new_terms)
                self._save_blacklist()
                logger.info(
                    f"Added {len(new_terms)} new terms to blacklist (total: {len(_blacklist_cache)})"
                )

            return {
                "success": True,
                "imported_terms": len(new_terms),
                "total_terms": len(_blacklist_cache),
                "overwrite": overwrite,
            }

        except Exception as e:
            logger.error(f"Failed to import blacklist: {e}")
            return {"success": False, "error": str(e)}


# Global blacklist manager instance
blacklist_manager = BlacklistManager()
