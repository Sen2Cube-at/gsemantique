# Contributing

Thanks for taking the time to contribute to gsemantique. Contributions are welcome and greatly appreciated. The following is a set of guidelines for contributing. Contributions can come in different forms, as we will outline below. You don't even have to know Python to be able to contribute!

## Creating issues

Opening [issues](https://github.com/fkroeber/gsemantique/issues) is also a form of contributing. They help us to improve the quality and user-friendliness of the package, as well as to build a community around the package.

Use **Issues** if

- You are using the package and something is not working as it should. In that case, use the [bug report template](https://github.com/fkroeber/gsemantique/blob/main/.github/ISSUE_TEMPLATE/bug_report.md). Please first check if its really a bug in `gsemantique`, and if there are not already open issues reporting the same bug.
- You have a request for a new feature. In that case, use the [feature request template](https://github.com/fkroeber/gsemantique/blob/main/.github/ISSUE_TEMPLATE/feature_request.md). Please first check if the feature is not already present, and if there are not already open issues requesting the same feature.

## Solving issues

If you know how to write Python you are welcome to contribute by solving open issues, for example by implementing new [features](https://github.com/fkroeber/gsemantique/labels/enhancement) or fixing [bugs](https://github.com/fkroeber/gsemantique/labels/bug). Solving issues does not always involve writing code, you can also help by improving and extending [documentation](https://github.com/fkroeber/gsemantique/labels/documentation).

In any case, the common code contributing workflow is:

#### 1. Cloning the GitHub repo

Clone this GitHub repo, or alternatively first fork it and then clone your forked version of the repo. After cloning, enter the cloned directory.

```
git clone https://github.com/fkroeber/gsemantique.git
cd gsemantique
```

#### 2. Creating a local development environment

Always create a new git branch to work in, instead of working in the main branch (if you have forked the repo first this is less important).

```
git checkout -b my_new_branch
```

It is recommended to create a new virtual environment containing all the dependencies of gsemantique, without disturbing your base environment.

#### 3. Writing & Formatting code

Please use [Flake8](https://flake8.pycqa.org/en/latest/) to check your code for style and syntax errors, and format your code by using [Black](https://github.com/psf/black). In case you are using VS Code, both packages are available as easy-to-use extensions, for further information find the VS code documentation for [linting](https://code.visualstudio.com/docs/python/linting) and [formatting](https://code.visualstudio.com/docs/python/formatting).

#### 4. Commiting code to your local branch

Don't forget to actually test your code before commiting. When commiting changes with `git commit` we try to use structured commit messages, adapted from https://www.conventionalcommits.org/. The first line of commit message should have the following format: 

```
<type>: <summary>
```

The summary should be short (preferably < 50 characters), starting with an upper case, and written in present tense. If the commit references a specific issue, include `Refs #<issue number>` in the summary. If the issue is a bug report, you may also use `Fix #<issue number>` such that the issue gets closed automatically.

The type should be one of the defined types listed below. If you feel artistic, you can end the commit message with the emoji belonging to the type |:sunglasses:|.

- **feat**: Implementation of a new feature. `:gift:` |:gift:|
- **fix**: A bug fix. `:wrench:` |:wrench:|
- **style**: Changes to code formatting. No change to program logic. `:art:` |:art:|
- **refactor**: Changes to code which do not change behaviour, e.g. renaming variables or splitting functions. `:construction:` |:construction:|
- **docs**: Adding, removing or updating user documentation or to code comments. `:books:` |:books:|
- **logs**: Adding, removing or updating log messages. `:sound:` |:sound:|
- **test**: Adding, removing or updating tests. No changes to user code. `:test_tube:` |:test_tube:|
- **cicd**: Adding, removing or updating CI/CD workflows. No changes to user code. `:robot:` |:robot:|
- **deps**: Adding, removing or updating dependencies. `:couple:` |:couple:|
- **release**: Preparing a release, e.g. updating version numbers. `:bookmark` |:bookmark:|
- **repo**: Changes to the repository that do not involve code/documentation, e.g. adding templates or community files. `:package:` |:package:|

Example commit messages are:

```
git commit -m 'feat: Add bar parameter to foo(), Refs #10 :gift:'
git commit -m 'fix: Include type checking in foo(), Fix #12 :wrench:'
```

#### 5. Pushing your branch to the GitHub repo

Please **never push directly to the main branch**!

```
git push origin my_new_branch
```

#### 6. Creating a pull request

Create a request to merge your changes into the main branch using the [Pull Request](https://github.com/fkroeber/gsemantique/pulls) functionality from GitHub. This should automatically provide you with the [pull request template](https://github.com/fkroeber/gsemantique/blob/main/.github/pull_request_template.md). Add at least one of the package maintainer as reviewer of your pull request, and make sure the automatic checks done by GitHub pass without errors.

Happy coding!
