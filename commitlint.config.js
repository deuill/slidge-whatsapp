// This files defines the allowed "headers" for the commit messages.
// Following the rules makes the changelog generation easier.
// They come from angular conventions, unless commented.
const Configuration = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum':
      [
        2,
        'always',
        [
          'build',
          'chore',
          'ci',
          'docs',
          'feat',
          'fix',
          'perf',
          'refactor',
          'revert',
          'style',
          'test',
          'compat',  // workaround to play nice with non-compliant clients or servers; ideally reverted once fixed upstream
          'cfix', // fixes an unreleased commit, should not appear in changelog
          'imprv',  // improvement of an existing feature
        ]
      ],
  },
}

module.exports = Configuration
