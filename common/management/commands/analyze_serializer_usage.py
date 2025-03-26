import os
import sys
import re
import importlib
import traceback
import inspect
from pathlib import Path
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.apps import apps


class SerializerUsageAnalyzer:
    def __init__(self, serializer_path):
        self.serializer_path = serializer_path
        self.app_name, self.file_name, self.serializer_name = self._parse_serializer_path()
        print(f"Looking for serializer {self.serializer_name} in {self.app_name}.{self.file_name}")
        self.serializer_class = None  # We'll set this in find_serializer_in_file method
        self.results = {
            "direct_imports": [],
            "serializer_class_declarations": [],
            "field_usages": [],
            "serializer_inheritance": [],
            "direct_instantiations": [],
            "many_true_usages": [],
            "inner_class_usages": [],
            "meta_class_references": []
        }
        # Keep track of results to avoid duplicates
        self.seen_results = set()
        
    def _parse_serializer_path(self):
        parts = self.serializer_path.split('.')
        if len(parts) < 3:
            raise ValueError(
                "Serializer path must be in format 'app.file_name.serializer_name' or 'app.path.to.file_name.serializer_name'"
            )
        
        # Handle case like procurementapi.rest.serializers.procure.ProcureModelSerializer
        if len(parts) > 3:
            # Last part is serializer_name, second to last is file_name, first part is app_name
            return parts[0], parts[-2], parts[-1]
        return parts[0], parts[1], parts[2]
    
    def find_serializer_in_file(self):
        """Find the serializer by directly checking files without importing"""
        # First try direct file approach - check if serializer exists in the file itself
        possible_file_patterns = [
            f"{self.app_name}/rest/serializers/{self.file_name}.py",
            f"{self.app_name}/{self.file_name}.py",
            f"{self.app_name}/serializers/{self.file_name}.py",
            # Additional paths for custom serializers
            f"{self.app_name}/custom_serializer/{self.file_name}.py"
        ]
        
        # Print patterns for debugging
        print(f"Looking for serializer {self.serializer_name} in these file patterns:")
        for pattern in possible_file_patterns:
            print(f"  - {pattern}")
        
        # Look for class definition in files
        for file_pattern in possible_file_patterns:
            try:
                if os.path.exists(file_pattern):
                    print(f"Found file: {file_pattern}")
                    with open(file_pattern, 'r', encoding='utf-8') as f:
                        content = f.read()
                        # Look for class definition
                        class_pattern = rf"class\s+{self.serializer_name}\("
                        if re.search(class_pattern, content):
                            print(f"Found serializer class {self.serializer_name} in {file_pattern}")
                            # Set found flag instead of actual class
                            self.serializer_class = True
                            return True
                        
                        # Also check for inner class definition
                        inner_class_pattern = rf"class\s+\w+Serializer.*?class\s+{self.serializer_name}\("
                        if re.search(inner_class_pattern, content, re.DOTALL):
                            print(f"Found {self.serializer_name} as inner class in {file_pattern}")
                            self.serializer_class = True
                            return True
            except (UnicodeDecodeError, IOError) as e:
                print(f"Error reading file {file_pattern}: {e}")
                continue
        
        # If direct check failed, try to find any parent serializer that might contain this one
        print(f"Could not find direct definition of {self.serializer_name}, checking for parent serializers...")
        parent_serializers = self._find_potential_parent_serializers()
        if parent_serializers:
            print(f"Found potential parent serializers: {', '.join(parent_serializers)}")
            self.serializer_class = True
            return True
            
        # Try checking all Python files in the app directory as a last resort
        print(f"Could not find direct definition of {self.serializer_name}, scanning all files in {self.app_name}...")
        app_dir = None
        for dir_path in self.project_dirs:
            if os.path.basename(dir_path) == self.app_name:
                app_dir = dir_path
                break
                
        if app_dir:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                # Look for inner class definition
                                inner_class_pattern = rf"class\s+\w+.*?class\s+{self.serializer_name}\("
                                if re.search(inner_class_pattern, content, re.DOTALL):
                                    print(f"Found {self.serializer_name} as inner class in {file_path}")
                                    self.serializer_class = True
                                    return True
                        except (UnicodeDecodeError, IOError):
                            continue
        
        print(f"Warning: Could not find serializer {self.serializer_name} in any files.")
        return False
    
    def analyze(self):
        """
        Analyze the codebase for all possible uses of the specified serializer
        """
        # Initialize project_dirs first
        self._find_project_roots()
        
        # Then try to find the serializer in files without importing
        if not self.find_serializer_in_file():
            print(f"Warning: Could not find serializer {self.serializer_name} in any files.")
            print("Continuing with analysis anyway, but results may not be accurate.")
        
        # First scan for direct imports
        self._scan_for_direct_imports()
        
        # Scan for serializer_class in views
        self._scan_for_serializer_class_declarations()
        
        # Scan for serializer field usages
        self._scan_for_field_usages()
        
        # Scan for inheritance
        self._scan_for_serializer_inheritance()
        
        # Scan for direct instantiations
        self._scan_for_direct_instantiations()
        
        # Scan for many=True usages
        self._scan_for_many_true_usages()
        
        # Scan for inner class usages like SerializerName.Inner
        self._scan_for_inner_class_usages()
        
        # Scan for Meta class references
        self._scan_for_meta_class_references()
        
        # Scan for usages of the serializer accessed through a parent serializer
        self._scan_for_parent_serializer_usages()
        
        # Deduplicate results
        self._deduplicate_results()
        
        return self.results
    
    def _deduplicate_results(self):
        """Remove duplicate entries from results"""
        for category in self.results:
            unique_results = []
            seen = set()
            
            for item in self.results[category]:
                # Create a tuple with file and line for deduplication
                key = (item["file"], item.get("line", 0))
                if key not in seen:
                    seen.add(key)
                    unique_results.append(item)
            
            self.results[category] = unique_results
        
    def _find_project_roots(self):
        """Find all Django app directories in the project"""
        django_apps = [app.path for app in apps.get_app_configs()]
        self.project_dirs = set(django_apps)
        
    def _scan_for_direct_imports(self):
        """Find direct imports of the serializer across the project"""
        import_patterns = [
            # from app.file_name import serializer_name
            rf"from\s+{self.app_name}\.{self.file_name}\s+import\s+.*{self.serializer_name}",
            # from app.file_name import (serializer_name, ...)
            rf"from\s+{self.app_name}\.{self.file_name}\s+import\s+\(.*{self.serializer_name}.*\)",
            # from app.serializers import serializer_name
            rf"from\s+{self.app_name}\.serializers\s+import\s+.*{self.serializer_name}",
            # from app.serializers import (serializer_name, ...)
            rf"from\s+{self.app_name}\.serializers\s+import\s+\(.*{self.serializer_name}.*\)",
            # from app.rest.serializers.file_name import serializer_name
            rf"from\s+{self.app_name}\.rest\.serializers\.{self.file_name}\s+import\s+.*{self.serializer_name}",
            # from app.rest.serializers import serializer_name
            rf"from\s+{self.app_name}\.rest\.serializers\s+import\s+.*{self.serializer_name}",
        ]
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                
                                for pattern in import_patterns:
                                    matches = re.findall(pattern, content)
                                    if matches:
                                        rel_path = os.path.relpath(file_path)
                                        self.results["direct_imports"].append({
                                            "file": rel_path,
                                            "import_stmt": matches[0]
                                        })
                        except (UnicodeDecodeError, IOError):
                            continue
    
    def _scan_for_serializer_class_declarations(self):
        """Find where the serializer is used as serializer_class in views"""
        patterns = [
            # Standard serializer_class assignment
            rf"serializer_class\s*=\s*{self.serializer_name}",
            rf"serializer_class\s*=\s*{self.serializer_name}\.[A-Za-z]+",
            # Dynamic serializer selection in get_serializer_class method
            rf"(?:return|yield)\s+{self.serializer_name}",
            # Assignment within get_serializer_class
            rf"serializer\s*=\s*{self.serializer_name}",
            # Serializer in a dictionary or list
            rf"['\"]{self.serializer_name}['\"]",
            rf":\s*{self.serializer_name}",
        ]
        
        # Check for get_serializer_class method definitions that might use this serializer
        get_serializer_pattern = r"def\s+get_serializer(?:_class)?\s*\([^)]*\):\s*(?:\n[^\n]*){0,20}?\n[ \t]+(?:return|yield)\s+([^()\n]*)"
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                line_num = 0
                                
                                # Check standard patterns line by line
                                for line in content.split('\n'):
                                    line_num += 1
                                    for pattern in patterns:
                                        if re.search(pattern, line):
                                            rel_path = os.path.relpath(file_path)
                                            self.results["serializer_class_declarations"].append({
                                                "file": rel_path,
                                                "line": line_num,
                                                "content": line.strip()
                                            })
                                
                                # Check for get_serializer_class methods that might contain this serializer
                                # but span multiple lines
                                for match in re.finditer(get_serializer_pattern, content):
                                    method_body = match.group(1)
                                    if self.serializer_name in method_body:
                                        # Find the line number of this return statement
                                        start_pos = match.start(1)
                                        line_count = content[:start_pos].count('\n') + 1
                                        
                                        rel_path = os.path.relpath(file_path)
                                        self.results["serializer_class_declarations"].append({
                                            "file": rel_path,
                                            "line": line_count,
                                            "content": f"Dynamic selection in get_serializer_class: {method_body.strip()}"
                                        })
                        except (UnicodeDecodeError, IOError):
                            continue
    
    def _scan_for_field_usages(self):
        """Find serializer used as a field in other serializers"""
        patterns = [
            rf"\w+\s*=\s*{self.serializer_name}\(",
            rf"\w+\s*=\s*{self.serializer_name}\.\w+\(",
        ]
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                line_num = 0
                                
                                for line in content.split('\n'):
                                    line_num += 1
                                    for pattern in patterns:
                                        if re.search(pattern, line):
                                            rel_path = os.path.relpath(file_path)
                                            self.results["field_usages"].append({
                                                "file": rel_path,
                                                "line": line_num,
                                                "content": line.strip()
                                            })
                        except (UnicodeDecodeError, IOError):
                            continue
    
    def _scan_for_serializer_inheritance(self):
        """Find serializers that inherit from this serializer"""
        pattern = rf"class\s+\w+\({self.serializer_name}.*\):"
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                line_num = 0
                                
                                for line in content.split('\n'):
                                    line_num += 1
                                    if re.search(pattern, line):
                                        rel_path = os.path.relpath(file_path)
                                        self.results["serializer_inheritance"].append({
                                            "file": rel_path,
                                            "line": line_num,
                                            "content": line.strip()
                                        })
                        except (UnicodeDecodeError, IOError):
                            continue
    
    def _scan_for_direct_instantiations(self):
        """Find direct instantiations of the serializer in code"""
        patterns = [
            rf"{self.serializer_name}\(",
            rf"{self.serializer_name}\.\w+\(",
        ]
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                line_num = 0
                                
                                for line in content.split('\n'):
                                    line_num += 1
                                    for pattern in patterns:
                                        # Avoid duplication with field_usages and class definitions
                                        if re.search(pattern, line) and "class" not in line and "=" not in line:
                                            rel_path = os.path.relpath(file_path)
                                            key = (rel_path, line_num, line.strip())
                                            if key not in self.seen_results:
                                                self.seen_results.add(key)
                                                self.results["direct_instantiations"].append({
                                                    "file": rel_path,
                                                    "line": line_num,
                                                    "content": line.strip()
                                                })
                        except (UnicodeDecodeError, IOError):
                            continue
    
    def _scan_for_many_true_usages(self):
        """Find instances where serializer is used with many=True"""
        patterns = [
            rf"{self.serializer_name}\(.*many=True",
            rf"{self.serializer_name}\.\w+\(.*many=True",
        ]
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                line_num = 0
                                
                                for line in content.split('\n'):
                                    line_num += 1
                                    for pattern in patterns:
                                        if re.search(pattern, line):
                                            rel_path = os.path.relpath(file_path)
                                            self.results["many_true_usages"].append({
                                                "file": rel_path,
                                                "line": line_num,
                                                "content": line.strip()
                                            })
                        except (UnicodeDecodeError, IOError):
                            continue
                            
    def _scan_for_inner_class_usages(self):
        """Find instances where inner classes of serializer are referenced"""
        # This pattern will find references to SerializerName.InnerClass
        pattern = rf"{self.serializer_name}\.[A-Za-z]+"
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                line_num = 0
                                
                                for line in content.split('\n'):
                                    line_num += 1
                                    if re.search(pattern, line) and "serializer_class" not in line:
                                        rel_path = os.path.relpath(file_path)
                                        self.results["inner_class_usages"].append({
                                            "file": rel_path,
                                            "line": line_num,
                                            "content": line.strip()
                                        })
                        except (UnicodeDecodeError, IOError):
                            continue
                            
    def _scan_for_meta_class_references(self):
        """Find instances where Meta class of serializer is referenced or subclassed"""
        pattern = rf"{self.serializer_name}\.Meta"
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                line_num = 0
                                
                                for line in content.split('\n'):
                                    line_num += 1
                                    if re.search(pattern, line):
                                        rel_path = os.path.relpath(file_path)
                                        self.results["meta_class_references"].append({
                                            "file": rel_path,
                                            "line": line_num,
                                            "content": line.strip()
                                        })
                        except (UnicodeDecodeError, IOError):
                            continue

    def _find_potential_parent_serializers(self):
        """Find serializers that might contain this one as an inner class"""
        parent_serializers = []
        potential_parent_pattern = rf"class\s+(\w+Serializer).*?class\s+{self.serializer_name}"
        
        for app_dir in self.project_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                for match in re.finditer(potential_parent_pattern, content, re.DOTALL):
                                    parent_serializer = match.group(1)
                                    parent_serializers.append(parent_serializer)
                        except (UnicodeDecodeError, IOError):
                            continue
        
        return parent_serializers

    def _scan_for_parent_serializer_usages(self):
        """Find usages where this serializer is accessed through its parent"""
        parent_serializers = self._find_potential_parent_serializers()
        
        if not parent_serializers:
            return
        
        # Look for parent_serializer.this_serializer
        for parent in parent_serializers:
            pattern = rf"{parent}\.{self.serializer_name}"
            for app_dir in self.project_dirs:
                for root, _, files in os.walk(app_dir):
                    for file in files:
                        if file.endswith('.py'):
                            file_path = os.path.join(root, file)
                            try:
                                with open(file_path, 'r', encoding='utf-8') as f:
                                    content = f.read()
                                    line_num = 0
                                    
                                    for line in content.split('\n'):
                                        line_num += 1
                                        if re.search(pattern, line):
                                            rel_path = os.path.relpath(file_path)
                                            self.results["serializer_class_declarations"].append({
                                                "file": rel_path,
                                                "line": line_num,
                                                "content": f"Used through parent: {line.strip()}"
                                            })
                            except (UnicodeDecodeError, IOError):
                                continue


class Command(BaseCommand):
    help = 'Analyze how a serializer is used throughout the project'

    def add_arguments(self, parser):
        # Add positional argument
        parser.add_argument(
            'serializer_path',
            help='Path to the serializer in format app.file_name.serializer_name'
        )
        
        # Add options without -v
        parser.add_argument(
            '--verbose', 
            action='store_true',
            dest='verbose',
            help='Show detailed information about each usage'
        )
        
        parser.add_argument(
            '--output',
            '-o',
            dest='output',
            help='Save results to a file'
        )

    def handle(self, *args, **options):
        serializer_path = options['serializer_path']
        verbose = options.get('verbose', False)
        output_file = options.get('output')
        
        try:
            analyzer = SerializerUsageAnalyzer(serializer_path)
            results = analyzer.analyze()
            
            # Prepare output
            output_lines = [f'Analysis of serializer {serializer_path}:']
            
            # Build summary
            summary = {
                "Direct imports": len(results["direct_imports"]),
                "Used as serializer_class": len(results["serializer_class_declarations"]),
                "Used as a field": len(results["field_usages"]),
                "Other serializers inherit from it": len(results["serializer_inheritance"]),
                "Direct instantiations": len(results["direct_instantiations"]),
                "Used with many=True": len(results["many_true_usages"]),
                "Inner class usages": len(results["inner_class_usages"]),
                "Meta class references": len(results["meta_class_references"])
            }
            
            # Add summary
            output_lines.append("\nSummary:")
            for key, value in summary.items():
                output_lines.append(f"  - {key}: {value}")
            
            # Add details if verbose
            if verbose:
                if results["direct_imports"]:
                    output_lines.append('\nDirect imports:')
                    for imp in results["direct_imports"]:
                        output_lines.append(f"  - {imp['file']}: {imp['import_stmt']}")
                else:
                    output_lines.append('\nNo direct imports found.')
                    
                if results["serializer_class_declarations"]:
                    output_lines.append('\nUsed as serializer_class in views:')
                    for decl in results["serializer_class_declarations"]:
                        output_lines.append(f"  - {decl['file']}:{decl['line']}: {decl['content']}")
                else:
                    output_lines.append('\nNot used as serializer_class in any view.')
                    
                if results["field_usages"]:
                    output_lines.append('\nUsed as a field in other serializers:')
                    for usage in results["field_usages"]:
                        output_lines.append(f"  - {usage['file']}:{usage['line']}: {usage['content']}")
                else:
                    output_lines.append('\nNot used as a field in other serializers.')
                    
                if results["serializer_inheritance"]:
                    output_lines.append('\nOther serializers inherit from this serializer:')
                    for inherit in results["serializer_inheritance"]:
                        output_lines.append(f"  - {inherit['file']}:{inherit['line']}: {inherit['content']}")
                else:
                    output_lines.append('\nNo serializers inherit from this serializer.')
                    
                if results["direct_instantiations"]:
                    output_lines.append('\nDirect instantiations:')
                    for inst in results["direct_instantiations"]:
                        output_lines.append(f"  - {inst['file']}:{inst['line']}: {inst['content']}")
                else:
                    output_lines.append('\nNo direct instantiations found.')
                    
                if results["many_true_usages"]:
                    output_lines.append('\nUsed with many=True:')
                    for usage in results["many_true_usages"]:
                        output_lines.append(f"  - {usage['file']}:{usage['line']}: {usage['content']}")
                else:
                    output_lines.append('\nNot used with many=True.')
                    
                if results["inner_class_usages"]:
                    output_lines.append('\nInner class usages:')
                    for usage in results["inner_class_usages"]:
                        output_lines.append(f"  - {usage['file']}:{usage['line']}: {usage['content']}")
                else:
                    output_lines.append('\nNo inner class usages found.')
                    
                if results["meta_class_references"]:
                    output_lines.append('\nMeta class references:')
                    for ref in results["meta_class_references"]:
                        output_lines.append(f"  - {ref['file']}:{ref['line']}: {ref['content']}")
                else:
                    output_lines.append('\nNo Meta class references found.')
            else:
                output_lines.append("\nRun with --verbose to see details of each usage.")
            
            # Print or save output
            if output_file:
                with open(output_file, 'w') as f:
                    f.write('\n'.join(output_lines))
                self.stdout.write(self.style.SUCCESS(f"Results saved to {output_file}"))
            else:
                for line in output_lines:
                    if line.startswith('\nNo ') or 'not' in line.lower():
                        self.stdout.write(self.style.WARNING(line))
                    else:
                        self.stdout.write(self.style.SUCCESS(line))
            
        except (ValueError, ImportError) as e:
            raise CommandError(str(e))
