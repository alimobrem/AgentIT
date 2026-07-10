from pathlib import Path

from agentit.analyzers.stack_detector import StackDetector


def test_detect_go_project(create_mock_repo):
    repo = create_mock_repo({
        "go.mod": "module github.com/test/app\n\ngo 1.22\n",
        "main.go": 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("hi") }\n',
        "handler.go": "package main\n",
    })
    detector = StackDetector()
    stack = detector.detect(repo)
    assert any(lang.name == "go" for lang in stack.languages)
    go_lang = next(l for l in stack.languages if l.name == "go")
    assert go_lang.version == "1.22"
    assert go_lang.file_count == 2
    assert "go mod" in stack.package_managers


def test_detect_python_project(create_mock_repo):
    repo = create_mock_repo({
        "requirements.txt": "flask==3.0.0\npsycopg2-binary==2.9.9\n",
        "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        "utils.py": "import os\n",
    })
    detector = StackDetector()
    stack = detector.detect(repo)
    assert any(lang.name == "python" for lang in stack.languages)
    assert any(fw.name == "flask" for fw in stack.frameworks)
    assert any(db.name == "postgresql" for db in stack.databases)
    assert "pip" in stack.package_managers


def test_detect_node_project(create_mock_repo):
    repo = create_mock_repo({
        "package.json": '{"name": "app", "dependencies": {"next": "14.0.0", "pg": "8.11.0"}}',
        "src/index.ts": "export default function Home() {}",
        "src/api.ts": "import pg from 'pg'",
    })
    detector = StackDetector()
    stack = detector.detect(repo)
    assert any(lang.name == "typescript" for lang in stack.languages)
    assert any(fw.name == "next.js" for fw in stack.frameworks)
    assert any(db.name == "postgresql" for db in stack.databases)
    assert "npm" in stack.package_managers


def test_detect_java_project(create_mock_repo):
    repo = create_mock_repo({
        "pom.xml": """<project>
  <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>3.2.0</version></parent>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
    <dependency><groupId>mysql</groupId><artifactId>mysql-connector-java</artifactId></dependency>
  </dependencies>
</project>""",
        "src/main/java/com/example/App.java": "package com.example;\npublic class App {}\n",
    })
    detector = StackDetector()
    stack = detector.detect(repo)
    assert any(lang.name == "java" for lang in stack.languages)
    assert any(fw.name == "spring boot" for fw in stack.frameworks)
    assert any(db.name == "mysql" for db in stack.databases)
    assert "maven" in stack.package_managers


def test_detect_empty_repo(create_mock_repo):
    repo = create_mock_repo({"README.md": "# Hello"})
    detector = StackDetector()
    stack = detector.detect(repo)
    assert len(stack.languages) == 0
    assert len(stack.frameworks) == 0
