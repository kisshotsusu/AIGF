# HomeAgent 独立项目工作区

HomeAgent 创建的独立软件项目默认存放在本目录，每个项目使用单独的英文子目录：

```text
Projects/
  project-name/
    README.md
    source files
    tests/
```

该目录不同于临时 `work/`，不会被 HomeAgent 的工作区定期清理任务删除。每个项目必须包含启动说明和可重复运行的自动测试。
