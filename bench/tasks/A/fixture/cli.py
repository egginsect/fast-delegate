#!/usr/bin/env python3
"""A simple CLI tool for managing tasks."""

import argparse
import sys

# Exit codes
EXIT_SUCCESS = 0
EXIT_INVALID_ARGS = 1
EXIT_NOT_FOUND = 2
EXIT_ALREADY_EXISTS = 3


def list_handler(args):
    """List all tasks."""
    print("Listing tasks...")
    return EXIT_SUCCESS


def create_handler(args):
    """Create a new task."""
    print(f"Creating task: {args.name}")
    return EXIT_SUCCESS


def delete_handler(args):
    """Delete an existing task."""
    print(f"Deleting task: {args.task_id}")
    return EXIT_SUCCESS


def update_handler(args):
    """Update an existing task."""
    print(f"Updating task: {args.task_id}")
    return EXIT_SUCCESS


def show_handler(args):
    """Show details of a task."""
    print(f"Showing task: {args.task_id}")
    return EXIT_SUCCESS


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Task management CLI")
    subparsers = parser.add_subparsers(dest="command")

    list_cmd = subparsers.add_parser("list", help="List all tasks")
    list_cmd.set_defaults(handler=list_handler)

    create_cmd = subparsers.add_parser("create", help="Create a new task")
    create_cmd.add_argument("name", help="Task name")
    create_cmd.set_defaults(handler=create_handler)

    delete_cmd = subparsers.add_parser("delete", help="Delete a task")
    delete_cmd.add_argument("task_id", help="Task ID")
    delete_cmd.set_defaults(handler=delete_handler)

    update_cmd = subparsers.add_parser("update", help="Update a task")
    update_cmd.add_argument("task_id", help="Task ID")
    update_cmd.set_defaults(handler=update_handler)

    show_cmd = subparsers.add_parser("show", help="Show task details")
    show_cmd.add_argument("task_id", help="Task ID")
    show_cmd.set_defaults(handler=show_handler)

    args = parser.parse_args()
    if not hasattr(args, "handler"):
        parser.print_help()
        return EXIT_INVALID_ARGS

    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
