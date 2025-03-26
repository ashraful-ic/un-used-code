import os
import re
import sys
import json
import time
import importlib
from collections import defaultdict
from pathlib import Path
from tqdm import tqdm

from django.core.management.base import BaseCommand, CommandError
from django.apps import apps

# Import our existing serializer analyzer
from common.management.commands.analyze_serializer_usage import SerializerUsageAnalyzer


class SerializerFinder:
    """Finds all serializer classes in the project"""
    
    def __init__(self):
        self.serializers = []
        self.app_dirs = [app.path for app in apps.get_app_configs()]
    
    def find_all_serializers(self):
        """Find all serializer classes in the project"""
        print("Scanning for serializers in project...")
        
        # Patterns to identify serializer classes
        serializer_patterns = [
            r"class\s+(\w+Serializer)\s*\(",  # Standard serializer class definition
            r"class\s+(\w+Serializer)\s*:",   # Class with no immediate parent
        ]
        
        for app_dir in self.app_dirs:
            for root, _, files in os.walk(app_dir):
                for file in files:
                    if file.endswith('.py'):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                
                                for pattern in serializer_patterns:
                                    for match in re.finditer(pattern, content):
                                        serializer_name = match.group(1)
                                        
                                        # Determine app and file name
                                        rel_path = os.path.relpath(file_path)
                                        parts = rel_path.split(os.sep)
                                        
                                        if len(parts) > 0:
                                            app_name = parts[0]
                                            file_name = os.path.splitext(parts[-1])[0]
                                            
                                            # Create serializer path
                                            if 'rest/serializers' in rel_path:
                                                serializer_path = f"{app_name}.rest.serializers.{file_name}.{serializer_name}"
                                            elif 'serializers' in rel_path:
                                                serializer_path = f"{app_name}.serializers.{file_name}.{serializer_name}"
                                            else:
                                                serializer_path = f"{app_name}.{file_name}.{serializer_name}"
                                            
                                            self.serializers.append({
                                                'name': serializer_name,
                                                'path': serializer_path,
                                                'file': rel_path
                                            })
                        except (UnicodeDecodeError, IOError):
                            continue
        
        # Remove duplicates
        unique_serializers = []
        seen = set()
        
        for serializer in self.serializers:
            if serializer['name'] not in seen:
                seen.add(serializer['name'])
                unique_serializers.append(serializer)
        
        self.serializers = unique_serializers
        print(f"Found {len(self.serializers)} serializers in the project.")
        return self.serializers


class Command(BaseCommand):
    help = 'Find all serializers in the project and identify which ones are not used'

    def add_arguments(self, parser):
        parser.add_argument(
            '--threshold', 
            type=int,
            default=0,
            help='Usage threshold below which a serializer is considered unused (default: 0)'
        )
        parser.add_argument(
            '--output',
            '-o',
            dest='output',
            help='Save results to a file'
        )
        parser.add_argument(
            '--verbose', 
            action='store_true',
            dest='verbose',
            help='Show detailed information about each serializer usage'
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Limit the number of serializers to analyze (useful for testing)'
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output results in JSON format'
        )
        parser.add_argument(
            '--app',
            help='Only analyze serializers in the specified app'
        )

    def handle(self, *args, **options):
        threshold = options.get('threshold', 0)
        output_file = options.get('output')
        verbose = options.get('verbose', False)
        limit = options.get('limit')
        use_json = options.get('json', False)
        target_app = options.get('app')
        
        start_time = time.time()
        
        # Find all serializers
        finder = SerializerFinder()
        serializers = finder.find_all_serializers()
        
        # Filter by app if specified
        if target_app:
            serializers = [s for s in serializers if s['path'].startswith(f"{target_app}.")]
            print(f"Filtered to {len(serializers)} serializers in app '{target_app}'")
            
        # Apply limit if specified
        if limit and limit > 0:
            serializers = serializers[:limit]
            print(f"Limiting analysis to first {limit} serializers")
            
        # Analyze each serializer
        unused_serializers = []
        all_results = []
        
        # Prepare for saving intermediate results
        intermediate_file = None
        if output_file:
            file_name, file_ext = os.path.splitext(output_file)
            intermediate_file = f"{file_name}_progress{file_ext}"
        
        # Setup progress bar
        progress_bar = tqdm(total=len(serializers), desc="Analyzing serializers")
        
        for i, serializer in enumerate(serializers):
            try:
                # Update progress description
                progress_bar.set_description(f"Analyzing {serializer['name']}")
                
                analyzer = SerializerUsageAnalyzer(serializer['path'])
                
                # Skip the class detection warning as we're just looking for usage
                original_stdout = sys.stdout
                sys.stdout = open(os.devnull, 'w')  # Silence output
                results = analyzer.analyze()
                sys.stdout = original_stdout  # Restore output
                
                total_usages = sum([
                    len(results["direct_imports"]),
                    len(results["serializer_class_declarations"]),
                    len(results["field_usages"]),
                    len(results["serializer_inheritance"]),
                    len(results["direct_instantiations"]),
                    len(results["many_true_usages"]),
                    len(results["inner_class_usages"]),
                    len(results["meta_class_references"])
                ])
                
                serializer_result = {
                    'name': serializer['name'],
                    'path': serializer['path'],
                    'file': serializer['file'],
                    'total_usages': total_usages,
                    'details': results
                }
                
                all_results.append(serializer_result)
                
                if total_usages <= threshold:
                    unused_serializers.append(serializer_result)
                
                # Save intermediate results every 10 serializers
                if intermediate_file and (i+1) % 10 == 0:
                    self._save_intermediate_results(intermediate_file, unused_serializers, all_results, use_json)
                    
                # Update progress bar
                progress_bar.update(1)
                
            except Exception as e:
                progress_bar.write(f"Error analyzing {serializer['name']}: {str(e)}")
                progress_bar.update(1)
        
        progress_bar.close()
        
        # Sort results by usage count (least used first)
        unused_serializers.sort(key=lambda x: x['total_usages'])
        
        # Calculate elapsed time
        elapsed_time = time.time() - start_time
        
        # Output results
        if use_json:
            self._output_json_results(unused_serializers, all_results, len(serializers), elapsed_time, output_file)
        else:
            self._output_text_results(unused_serializers, all_results, len(serializers), elapsed_time, verbose, output_file)
        
    def _save_intermediate_results(self, file_path, unused_serializers, all_results, use_json):
        """Save intermediate results while processing"""
        try:
            if use_json:
                data = {
                    'unused_serializers': unused_serializers,
                    'analyzed_so_far': len(all_results),
                    'total_found': len(all_results) + 1,  # Add one for current serializer
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                with open(file_path, 'w') as f:
                    json.dump(data, f, indent=2)
            else:
                with open(file_path, 'w') as f:
                    f.write(f"Progress update - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"Analyzed {len(all_results)} serializers so far\n")
                    f.write(f"Found {len(unused_serializers)} unused serializers so far\n\n")
                    for serializer in unused_serializers:
                        f.write(f"{serializer['name']} - {serializer['file']} - Total usages: {serializer['total_usages']}\n")
        except Exception as e:
            print(f"Warning: Could not save intermediate results: {e}")
            
    def _output_json_results(self, unused_serializers, all_results, total_serializers, elapsed_time, output_file):
        """Output results in JSON format"""
        data = {
            'stats': {
                'total_serializers': total_serializers,
                'unused_serializers': len(unused_serializers),
                'elapsed_time_seconds': elapsed_time,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            },
            'unused_serializers': unused_serializers
        }
        
        if output_file:
            with open(output_file, 'w') as f:
                json.dump(data, f, indent=2)
            self.stdout.write(self.style.SUCCESS(f"Results saved to {output_file}"))
        else:
            self.stdout.write(json.dumps(data, indent=2))
    
    def _output_text_results(self, unused_serializers, all_results, total_serializers, elapsed_time, verbose, output_file):
        """Output results in text format"""
        # Format time nicely
        m, s = divmod(int(elapsed_time), 60)
        h, m = divmod(m, 60)
        time_str = f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        
        output_lines = [
            "Potentially unused serializers in the project:",
            f"Analysis completed in {time_str} (h:m:s)"
        ]
        
        if unused_serializers:
            for serializer in unused_serializers:
                output_lines.append(f"\n{serializer['name']} - {serializer['file']} - Total usages: {serializer['total_usages']}")
                
                if verbose:
                    details = serializer['details']
                    output_lines.append("  Usage breakdown:")
                    output_lines.append(f"  - Direct imports: {len(details['direct_imports'])}")
                    output_lines.append(f"  - Used as serializer_class: {len(details['serializer_class_declarations'])}")
                    output_lines.append(f"  - Used as a field: {len(details['field_usages'])}")
                    output_lines.append(f"  - Other serializers inherit from it: {len(details['serializer_inheritance'])}")
                    output_lines.append(f"  - Direct instantiations: {len(details['direct_instantiations'])}")
                    output_lines.append(f"  - Used with many=True: {len(details['many_true_usages'])}")
                    output_lines.append(f"  - Inner class usages: {len(details['inner_class_usages'])}")
                    output_lines.append(f"  - Meta class references: {len(details['meta_class_references'])}")
        else:
            output_lines.append("\nNo unused serializers found in the project.")
        
        # Summary of all serializers
        output_lines.append(f"\nTotal serializers found: {total_serializers}")
        output_lines.append(f"Potentially unused serializers: {len(unused_serializers)}")
        
        # Print or save output
        if output_file:
            with open(output_file, 'w') as f:
                f.write('\n'.join(output_lines))
            self.stdout.write(self.style.SUCCESS(f"Results saved to {output_file}"))
        else:
            for line in output_lines:
                if "No unused serializers found" in line:
                    self.stdout.write(self.style.SUCCESS(line))
                elif "unused serializer" in line.lower():
                    self.stdout.write(self.style.WARNING(line))
                else:
                    self.stdout.write(line)
